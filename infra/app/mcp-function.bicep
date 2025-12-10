metadata description = 'Create MCP Server Azure Function App'
param name string
param location string = resourceGroup().location
param tags object = {}

@description('Application Insights connection string')
param applicationInsightsConnectionString string

@description('User-assigned managed identity name')
param identityName string

@description('PostgreSQL connection string')
@secure()
param postgresConnectionString string

@description('Azure OpenAI endpoint')
param openAiEndpoint string

@description('Azure OpenAI embedding deployment name')
param embeddingDeployment string = 'text-embedding-ada-002'

var resourceToken = uniqueString(resourceGroup().id, name)

// MCP Server Function App
module functionApp '../core/host/functions.bicep' = {
  name: '${name}-function'
  params: {
    name: name
    location: location
    tags: tags
    appServicePlanName: 'plan-mcp-${resourceToken}'
    storageAccountName: 'stmcp${resourceToken}'
    identityName: identityName
    applicationInsightsConnectionString: applicationInsightsConnectionString
    appSettings: [
      {
        name: 'POSTGRES_CONNECTION_STRING'
        value: postgresConnectionString
      }
      {
        name: 'AZURE_OPENAI_ENDPOINT'
        value: openAiEndpoint
      }
      {
        name: 'AZURE_OPENAI_EMBEDDING_DEPLOYMENT'
        value: embeddingDeployment
      }
    ]
  }
}

output id string = functionApp.outputs.id
output name string = functionApp.outputs.name
output uri string = functionApp.outputs.uri
output identityPrincipalId string = functionApp.outputs.identityPrincipalId
output storageAccountName string = 'stmcp${resourceToken}'
