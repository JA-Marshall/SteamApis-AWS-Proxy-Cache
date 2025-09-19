import json
import os
import logging
import boto3
from botocore.exceptions import ClientError
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import time
from decimal import Decimal


class SteamApisError(Exception):
    """Custom exception for SteamApis errors with detailed information"""
    def __init__(self, message, status_code=None, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details

logger = logging.getLogger()
logger.setLevel("INFO")

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('SteamApis-item-Cache')

steamapis_key = os.environ['steamapis_key']

# Configure requests session with retry strategy
session = requests.Session()
retry_strategy = Retry(
    total=3,
    status_forcelist=[429, 500, 502, 503, 504],
    method_whitelist=["HEAD", "GET", "OPTIONS"],
    backoff_factor=1
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)


def get_from_cache(app_id, market_hash_name):
    """Get item from DynamoDB cache. Returns data if found, None if not."""
    try:
        response = table.get_item(Key={
            'app_id': app_id,
            'market_hash_name': market_hash_name
        })
        if 'Item' in response:
            return response['Item']
        return None
    except ClientError as e:
        logger.error(f"Error reading from cache: {e}")
        return None

def get_data_from_steamapis(app_id, market_hash_name):
    """Fetch data from SteamApis with proper error handling and timeouts"""
    url = f'https://api.steamapis.com/market/item/{app_id}/{market_hash_name}'
    params = {"api_key": steamapis_key}

    try:
        response = session.get(url, params=params, timeout=(5, 15))  # 5s connect, 15s read
        response.raise_for_status()

        data = response.json()
        return parse_market_data(data, app_id, market_hash_name)

    except requests.exceptions.Timeout:
        logger.error(f"Timeout calling SteamApis for {app_id}/{market_hash_name}")
        raise SteamApisError(
            "request_timeout",
            status_code=504,
            details={"message": "SteamApis request timeout"}
        )

    except requests.exceptions.HTTPError as e:
        logger.error(f"SteamApis request failed for {market_hash_name}with status {response.status_code} {response.text}")

        # Try to parse SteamApis error response
        steamapis_error = None
        try:
            steamapis_error = response.json()
        except (ValueError, json.JSONDecodeError):
            steamapis_error = {"error": response.text or "Unknown error"}

        if response.status_code == 400:
            raise SteamApisError(
                "item_not_found",
                status_code=400,
                details={
                    "message": steamapis_error.get("error", "No matching item found with these parameters"),
                    "steamapis_response": steamapis_error
                }
            )
        elif response.status_code == 404:
            raise SteamApisError(
                "item_not_found",
                status_code=404,
                details={
                    "message": steamapis_error.get("error", "Item not found on Steam market"),
                    "steamapis_response": steamapis_error
                }
            )
        elif response.status_code == 429:
            raise SteamApisError(
                "rate_limit_exceeded",
                status_code=429,
                details={
                    "message": steamapis_error.get("error", "Rate limit exceeded"),
                    "type": steamapis_error.get("type"),
                    "requests": steamapis_error.get("requests"),
                    "steamapis_response": steamapis_error
                }
            )
        else:
            raise SteamApisError(
                "steamapis_error",
                status_code=response.status_code,
                details={
                    "message": steamapis_error.get("error", f"SteamApis HTTP error: {response.status_code}"),
                    "steamapis_response": steamapis_error
                }
            )

    except requests.exceptions.RequestException as e:
        logger.error(f"Request error for {app_id}/{market_hash_name}: {str(e)}")
        raise SteamApisError(
            "network_error",
            status_code=500,
            details={"message": "Network error calling SteamApis"}
        )

    except ValueError as e:
        logger.error(f"JSON decode error for {app_id}/{market_hash_name}: {str(e)}")
        raise SteamApisError(
            "invalid_response",
            status_code=500,
            details={"message": "Invalid JSON response from SteamApis"}
        )


def parse_market_data(data, app_id, market_hash_name):
    """extract the data we want from the big resposne"""

    highest_buy_order = data.get("histogram", {}).get("highest_buy_order")
    lowest_sell_order = data.get("histogram", {}).get("lowest_sell_order")

    if highest_buy_order is None or lowest_sell_order is None:
        logger.error(
            f"Missing market data in response for {app_id}/{market_hash_name} "
            f" Response: {data}"
        )
        raise SteamApisError(
            "missing_market_data",
            status_code=500,
            details={
                "message": "Missing market data in SteamApis response",
                "steamapis_response": data
            }
        )

    return {
        "app_id": app_id,
        "market_hash_name": market_hash_name,
        "highest_buy_order": Decimal(str(highest_buy_order)) if highest_buy_order is not None else None,
        "lowest_sell_order": Decimal(str(lowest_sell_order)) if lowest_sell_order is not None else None,
    }

def write_to_ddb_cache(market_data):
    """write market data to ddb cache with TTL"""
    try:
        market_data['ttl'] = int(time.time()) + 86400 #24 hours from now

        table.put_item(Item=market_data)
        logger.info(f"Cached data for {market_data['app_id']}/{market_data['market_hash_name']}")

    except ClientError as e:
        logger.error(f"Error saving to cache: {e}")



def get_market_data(app_id, market_hash_name):
    """Main function for getting data, reads cache first and then falls back to API"""
    cached_data = get_from_cache(app_id, market_hash_name)
    if cached_data:
        logger.info(f"Read from cache for {app_id}/{market_hash_name}")
        return cached_data

    market_data = get_data_from_steamapis(app_id, market_hash_name)

    write_to_ddb_cache(market_data)

    return market_data

    


def lambda_handler(event, context):
    """
    Main Lambda handler function
    Parameters:
        event: Dict containing the Lambda function event data
        context: Lambda runtime context
    Returns:
        Dict containing status message
    """
    try:
        path_parameters = event.get('pathParameters', {})
        app_id = path_parameters.get('app_id')
        market_hash_name = path_parameters.get('market_hash_name')

        if not app_id or not market_hash_name:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'app_id and market_hash_name are required'})
            }

        market_data = get_market_data(app_id, market_hash_name)
        
        # Convert Decimal back to float for JSON response
        response_data = {
            "app_id": market_data["app_id"],
            "market_hash_name": market_data["market_hash_name"],
            "highest_buy_order": float(market_data["highest_buy_order"]) if market_data.get("highest_buy_order") else None,
            "lowest_sell_order": float(market_data["lowest_sell_order"]) if market_data.get("lowest_sell_order") else None,
        }

        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps(response_data)
        }

    except SteamApisError as e:
        logger.error(f"SteamApis error for {app_id}/{market_hash_name}: {str(e)}")

        # Return detailed error response from SteamApis
        error_response = {
            'error': str(e),
            'message': e.details.get('message') if e.details else str(e)
        }

        # Add additional details if available
        if e.details:
            if e.details.get('type'):
                error_response['type'] = e.details['type']
            if e.details.get('requests'):
                error_response['requests'] = e.details['requests']
            if e.details.get('steamapis_response'):
                error_response['steamapis_response'] = e.details['steamapis_response']

        return {
            'statusCode': e.status_code or 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps(error_response)
        }

    except Exception as e:
        error_message = str(e)
        logger.error(f"Unexpected error processing request for {app_id}/{market_hash_name}: {error_message}")

        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({
                'error': 'internal_server_error',
                'message': 'An unexpected error occurred'
            })
        }