import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as path from 'path';

export class SteamApisCachingProxyStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const cacheTable = new dynamodb.Table(this, 'SteamApis-item-Cache', {
      tableName: 'SteamApis-item-Cache',
      partitionKey: {
        name: 'app_id',
        type: dynamodb.AttributeType.STRING
      },
      sortKey: {
        name: 'market_hash_name',
        type: dynamodb.AttributeType.STRING
      },
      timeToLiveAttribute: 'ttl',
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY
    });

    // Use AWS managed requests layer
    const requestsLayer = lambda.LayerVersion.fromLayerVersionArn(
      this,
      'RequestsLayer',
      'arn:aws:lambda:eu-west-2:336392948345:layer:AWSSDKPandas-Python311:9'
    );

    const priceFetcherLambda = new lambda.Function(this, 'PriceFetcherLambda', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'price_fetcher.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda')),
      layers: [requestsLayer],
      timeout: cdk.Duration.seconds(30),
      environment: {
        steamapis_key: process.env.STEAMAPIS_KEY || 'your-steamapis-key-here'
      }
    });

    cacheTable.grantReadWriteData(priceFetcherLambda);

    // API Gateway REST API (v1) for better API key support
    const api = new apigateway.RestApi(this, 'SteamApisCachingProxyApi', {
      restApiName: 'Steam APIs Caching Proxy',
      description: 'REST API Gateway that caches Steam API responses using DynamoDB',
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: ['Content-Type', 'X-Amz-Date', 'Authorization', 'X-Api-Key']
      },
      apiKeySourceType: apigateway.ApiKeySourceType.HEADER
    });

    // Create API key
    const apiKey = new apigateway.ApiKey(this, 'SteamApiKey', {
      apiKeyName: 'steam-apis-proxy-key',
      description: 'API Key for Steam APIs Caching Proxy'
    });

    // Create usage plan
    const usagePlan = new apigateway.UsagePlan(this, 'SteamApiUsagePlan', {
      name: 'steam-apis-proxy-plan',
      description: 'Usage plan for Steam APIs Caching Proxy',
      throttle: {
        rateLimit: 100,
        burstLimit: 200
      },
      quota: {
        limit: 10000,
        period: apigateway.Period.DAY
      }
    });

    // Associate API key with usage plan
    usagePlan.addApiKey(apiKey);
    usagePlan.addApiStage({
      api: api,
      stage: api.deploymentStage
    });

    // Lambda integration
    const lambdaIntegration = new apigateway.LambdaIntegration(priceFetcherLambda);

    // API resources with API key requirement
    const itemResource = api.root.addResource('item');
    const appIdResource = itemResource.addResource('{app_id}');
    const marketHashNameResource = appIdResource.addResource('{market_hash_name}');

    marketHashNameResource.addMethod('GET', lambdaIntegration, {
      apiKeyRequired: true
    });

    new cdk.CfnOutput(this, 'ApiGatewayUrl', {
      value: api.url,
      description: 'API Gateway endpoint URL'
    });

    new cdk.CfnOutput(this, 'ApiKey', {
      value: apiKey.keyId,
      description: 'API Key ID (use "aws apigateway get-api-key --api-key {this-value} --include-value" to get the actual key)'
    });

    new cdk.CfnOutput(this, 'CacheTableName', {
      value: cacheTable.tableName,
      description: 'DynamoDB cache table name'
    });
  }
}
