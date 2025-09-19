# SteamApis Caching Proxy

A minimal AWS CDK stack that deploys a serverless, read-through cache for the SteamApis `/market/item/{app_id}/{market_hash_name}` endpoint.

**Why cache?** SteamApis charges per API request. This proxy fronts SteamApis with a secure REST API (API Gateway + API Key → Lambda) and stores responses in DynamoDB with a 24-hour TTL, dramatically reducing your SteamApis costs by serving repeated requests from cache instead of making expensive API calls.

## What you get

- **REST API**: `GET /item/{app_id}/{market_hash_name}` with API key authentication
- **Lambda (Python 3.11)**: fetches from cache first; on miss, calls SteamApis and writes to DynamoDB
- **DynamoDB**: pay-per-request table with composite key and TTL (`ttl`) for automatic expiry
- **Security**: API key required, rate limiting, and usage quotas
- **Outputs**: API Gateway base URL + API key + DynamoDB table name

## Architecture

```
Client ──▶ Amazon API Gateway (REST API + API Key)
                  │
                  ▼
           AWS Lambda (Python)
          /        \
  DynamoDB (cache)  SteamApis (on miss)
```

- **Cache key**: `{app_id, market_hash_name}` (composite key prevents collisions)
- **TTL**: 24 hours from write time (configurable in code)
- **Security**: API key required for all requests

## API

### `GET /item/{app_id}/{market_hash_name}`

**Headers Required:**
- `X-API-Key: your-api-key-here`

**Response 200**
```json
{
  "app_id": "730",
  "market_hash_name": "AK-47 | Redline (Field-Tested)",
  "highest_buy_order": 5.67,
  "lowest_sell_order": 6.12
}
```

**Response 400**
```json
{ "error": "app_id and market_hash_name are required" }
```

**Response 403**
```json
{ "message": "Forbidden" }
```

**Response 404**
```json
{ "error": "Internal server error" }
```

**Response 429**
```json
{ "error": "Internal server error" }
```

**Response 500**
```json
{ "error": "Internal server error" }
```

## Requirements

- Node.js 18+ (CDK v2)
- AWS CDK v2 (`npm i -g aws-cdk`)
- An AWS account & credentials configured (`aws configure`)
- Python 3.11 runtime available for Lambda
- A SteamApis API key

### About Python dependencies

The Lambda imports `requests`. This project uses an AWS-managed layer to provide common Python libs:

```
arn:aws:lambda:eu-west-2:336392948345:layer:AWSSDKPandas-Python311:9
```

This layer is referenced in `lib/steam_apis-caching-proxy-stack.ts`. It includes `requests`.
If you deploy to a different region or prefer to vendor your own deps, see "Alternative: bundle your own dependencies" below.

## Deployment

### Install dependencies
```bash
npm ci
```

### (First time in an account/region) Bootstrap CDK
```bash
npx cdk bootstrap aws://<ACCOUNT_ID>/<REGION>
# example:
# npx cdk bootstrap aws://123456789012/eu-west-2
```

### Deploy (provide your SteamApis key)
```bash
# macOS/Linux
export STEAMAPIS_KEY="<your-steamapis-key>"
npx cdk deploy

# PowerShell
$env:STEAMAPIS_KEY="<your-steamapis-key>"
npx cdk deploy
```

### Grab the outputs
CDK will print:
- **ApiGatewayUrl** – e.g. `https://abc123.execute-api.eu-west-2.amazonaws.com/`
- **ApiKey** – Key ID to retrieve the actual API key
- **CacheTableName** – e.g. `SteamApis-item-Cache`

### Get your API key
```bash
# Use the ApiKey ID from CDK output
aws apigateway get-api-key --api-key <ApiKey-ID-from-output> --include-value

# Save the "value" field - this is your API key
```

## Quick start: test the endpoint
```bash
BASE_URL="https://abc123.execute-api.eu-west-2.amazonaws.com"
API_KEY="your-api-key-here"  # From aws apigateway get-api-key command
APP_ID="730"  # CS:GO / CS2
NAME="AK-47%20%7C%20Redline%20%28Field-Tested%29"

curl -s -H "X-API-Key: $API_KEY" "$BASE_URL/item/$APP_ID/$NAME" | jq
```

- **First request**: likely a cache miss → Lambda calls SteamApis, writes to DynamoDB, returns data.
- **Subsequent requests (≤ 24h)**: cache hit → DynamoDB result returned.

## Project layout
```
.
├─ bin/steam_apis-caching-proxy.ts          # CDK app entrypoint
├─ lib/steam_apis-caching-proxy-stack.ts    # CDK stack (API, Lambda, DynamoDB, outputs)
├─ lambda/
│  ├─ price_fetcher.py                      # Lambda handler + cache logic
│  └─ requirements.txt                      # (optional, if you bundle your own deps)
├─ package.json / package-lock.json
└─ README.md
```

## Configuration & behaviour

- **Environment**: `steamapis_key` is injected into the Lambda from your shell when you run `cdk deploy`.
- **Cache TTL**: hard-coded to 24 hours in `write_to_ddb_cache`:
  ```python
  market_data['ttl'] = int(time.time()) + 86400
  ```
  Adjust to taste (e.g., `3600` for 1 hour).
- **CORS**: `*` for origins, `GET` and `OPTIONS` allowed. Tighten before production use.
- **Timeouts**: Lambda timeout is 30s. SteamApis call is a simple `requests.get(...)` with default timeout; you may wish to set an explicit timeout.

## Costs

- **DynamoDB**: On-demand (`PAY_PER_REQUEST`). You're billed per read/write and for storage (TTL deletes items automatically).
- **Lambda**: Per-ms execution + requests.
- **API Gateway (HTTP API)**: Per request.


## Troubleshooting


### `403/401 from SteamApis`
- Check `STEAMAPIS_KEY` is valid and set at deploy time.
- Confirm the Lambda environment variable resolved: look in the Lambda console → Configuration → Environment variables.


## Design notes & caveats

### Cache key shape
The DynamoDB table uses a composite key structure:
```
partitionKey: { name: 'app_id', type: STRING }
sortKey: { name: 'market_hash_name', type: STRING }
```

This prevents cache collisions between different `app_id`s that might have the same `market_hash_name`.

### Error handling
The Lambda now returns appropriate HTTP status codes:
- **404**: Item not found on Steam market
- **429**: Rate limit exceeded
- **504**: Request timeout
- **500**: Other errors

All errors are logged to CloudWatch with detailed context.

### Timeouts & retries
The Lambda includes:
- **Request timeouts**: 5s connect, 15s read
- **Automatic retries**: 3 attempts with exponential backoff
- **Retry conditions**: 429, 5xx status codes

## Alternative: bundle your own dependencies (no external layer)

If you don't want to rely on the AWS-managed layer (or you're in a region without it), bundle Python deps into the Lambda asset:

Replace the layer in the CDK stack with a bundling step, or use AWS CDK's Python function construct. Example (Node CDK) sketch:

```typescript
const priceFetcherLambda = new lambda.Function(this, 'PriceFetcherLambda', {
  runtime: lambda.Runtime.PYTHON_3_11,
  handler: 'price_fetcher.lambda_handler',
  code: lambda.Code.fromAsset(path.join(__dirname, '../lambda'), {
    bundling: {
      image: lambda.Runtime.PYTHON_3_11.bundlingImage,
      command: [
        'bash', '-c',
        [
          'pip install -r requirements.txt -t /asset-output',
          'cp -r . /asset-output'
        ].join(' && ')
      ],
    },
  }),
  timeout: cdk.Duration.seconds(30),
  environment: { steamapis_key: process.env.STEAMAPIS_KEY ?? '' },
});
```

Ensure `lambda/requirements.txt` includes:
```
requests
boto3
```

Deploy as normal.

## License

MIT (or choose your own). Add a LICENSE file if you intend to open-source.
