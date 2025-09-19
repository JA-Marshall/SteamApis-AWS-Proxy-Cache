#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { SteamApisCachingProxyStack } from '../lib/steam_apis-caching-proxy-stack';

const app = new cdk.App();
new SteamApisCachingProxyStack(app, 'SteamApisCachingProxyStack', {
env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION
}});
