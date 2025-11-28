metadata description = 'Create Agent API Azure Function App'
param name string
param location string = resourceGroup().location
param tags object = {}

@description('Application Insights connection string')
param applicationInsightsConnectionString string

@description('User-assigned managed identity name')
param identityName string

@description('Azure OpenAI endpoint')
param openAiEndpoint string

@description('Azure OpenAI deployment name')
param openAiDeploymentName string

@description('MCP Server URL')
param mcpServerUrl string

var resourceToken = uniqueString(resourceGroup().id, name)

// Agent API Function App
module functionApp '../core/host/functions.bicep' = {
  name: '${name}-function'
  params: {
    name: name
    location: location
    tags: tags
    appServicePlanName: 'plan-agent-${resourceToken}'
    storageAccountName: 'stagent${resourceToken}'
    identityName: identityName
    applicationInsightsConnectionString: applicationInsightsConnectionString
    appSettings: [
      {
        name: 'AZURE_OPENAI_ENDPOINT'
        value: openAiEndpoint
      }
      {
        name: 'AZURE_OPENAI_DEPLOYMENT'
        value: openAiDeploymentName
      }
      {
        name: 'MCP_SERVER_URL'
        value: mcpServerUrl
      }
    ]
  }
}

output id string = functionApp.outputs.id
output name string = functionApp.outputs.name
output uri string = functionApp.outputs.uri
output identityPrincipalId string = functionApp.outputs.identityPrincipalId
output storageAccountName string = 'stagent${resourceToken}'
