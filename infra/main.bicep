targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment (used to generate resource names)')
param environmentName string

@minLength(1)
@description('Primary location for all resources')
param location string

@minLength(1)
@description('Location for PostgreSQL (may differ due to quota restrictions)')
param postgresLocation string = 'eastus2'

@description('Id of the principal to assign application roles')
param principalId string = ''

@description('Azure OpenAI model deployment name')
param openAiDeploymentName string = 'gpt-5-mini'

@description('Azure OpenAI model name')
param openAiModelName string = 'gpt-5-mini'

@description('Azure OpenAI model version')
param openAiModelVersion string = '2025-08-07'

@description('Azure OpenAI model capacity')
param openAiModelCapacity int = 100

var abbrs = loadJsonContent('./abbreviations.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }

// Organize resources in a resource group
resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: '${abbrs.resourcesResourceGroups}${environmentName}'
  location: location
  tags: tags
}

// User-assigned managed identity for Container Apps
module managedIdentity 'core/security/managed-identity.bicep' = {
  name: 'managed-identity'
  scope: rg
  params: {
    name: '${abbrs.managedIdentityUserAssignedIdentities}${resourceToken}'
    location: location
    tags: tags
  }
}

// Monitoring (Application Insights + Log Analytics)
module monitoring 'core/monitor/monitoring.bicep' = {
  name: 'monitoring'
  scope: rg
  params: {
    location: location
    tags: tags
    logAnalyticsName: '${abbrs.operationalInsightsWorkspaces}${resourceToken}'
    applicationInsightsName: '${abbrs.insightsComponents}${resourceToken}'
  }
}

// Container Registry for Container Apps
module containerRegistry 'core/host/container-registry.bicep' = {
  name: 'container-registry'
  scope: rg
  params: {
    name: '${abbrs.containerRegistryRegistries}${resourceToken}'
    location: location
    tags: tags
  }
}

// Container Apps Environment
module containerAppsEnvironment 'core/host/container-apps-environment.bicep' = {
  name: 'container-apps-environment'
  scope: rg
  params: {
    name: '${abbrs.appManagedEnvironments}${resourceToken}'
    location: location
    tags: tags
    logAnalyticsWorkspaceName: monitoring.outputs.logAnalyticsWorkspaceName
  }
}

// Azure OpenAI
module openAi 'core/ai/cognitiveservices.bicep' = {
  name: 'openai'
  scope: rg
  params: {
    name: '${abbrs.cognitiveServicesAccounts}${resourceToken}'
    location: location
    tags: tags
    sku: {
      name: 'S0'
    }
    deployments: [
      {
        name: openAiDeploymentName
        model: {
          format: 'OpenAI'
          name: openAiModelName
          version: openAiModelVersion
        }
        sku: {
          name: 'GlobalStandard'
          capacity: openAiModelCapacity
        }
      }
      {
        name: 'text-embedding-ada-002'
        model: {
          format: 'OpenAI'
          name: 'text-embedding-ada-002'
          version: '2'
        }
        sku: {
          name: 'Standard'
          capacity: 100
        }
      }
    ]
  }
}

// PostgreSQL Database with pgvector
var postgresAdminPassword = '${uniqueString(resourceToken)}Pg#'
module postgres 'app/postgres.bicep' = {
  name: 'postgres'
  scope: rg
  params: {
    name: '${abbrs.dBforPostgreSQLServers}${resourceToken}'
    location: postgresLocation
    tags: tags
    administratorLogin: 'pgadmin'
    administratorPassword: postgresAdminPassword
    databaseName: 'zava'
    allowAzureServices: true
  }
}

// MCP Server (Container App)
param mcpServerExists bool
module mcpServer 'app/mcp-containerapp.bicep' = {
  name: 'mcp-server'
  scope: rg
  params: {
    name: 'ca-mcp-${resourceToken}'
    location: location
    tags: union(tags, { 'azd-service-name': 'mcp-server' })
    identityName: managedIdentity.outputs.name
    containerAppsEnvironmentName: containerAppsEnvironment.outputs.name
    containerRegistryName: containerRegistry.outputs.name
    applicationInsightsConnectionString: monitoring.outputs.applicationInsightsConnectionString
    postgresConnectionString: postgres.outputs.connectionString
    openAiEndpoint: openAi.outputs.endpoint
    embeddingDeployment: 'text-embedding-ada-002'
    exists: mcpServerExists
  }
}

// Grant Container Registry pull access to managed identity
module acrPullRole 'core/security/role.bicep' = {
  scope: rg
  name: 'acr-pull-role'
  params: {
    principalId: managedIdentity.outputs.principalId
    roleDefinitionId: '7f951dda-4ed3-4680-a7ca-43fe172d538d' // AcrPull
    principalType: 'ServicePrincipal'
  }
}

// Agent Container App
param agentExists bool
module agent 'app/agent-containerapp.bicep' = {
  name: 'agent'
  scope: rg
  params: {
    name: 'ca-agent-${resourceToken}'
    location: location
    tags: union(tags, { 'azd-service-name': 'agent' })
    identityName: managedIdentity.outputs.name
    containerAppsEnvironmentName: containerAppsEnvironment.outputs.name
    containerRegistryName: containerRegistry.outputs.name
    applicationInsightsConnectionString: monitoring.outputs.applicationInsightsConnectionString
    openAiEndpoint: openAi.outputs.endpoint
    openAiDeploymentName: openAiDeploymentName
    mcpServerUrl: mcpServer.outputs.uri
    exists: agentExists
  }
}

// Role assignments for managed identity
module openAiRoleUser 'core/security/role.bicep' = {
  scope: rg
  name: 'openai-role-user'
  params: {
    principalId: managedIdentity.outputs.principalId
    roleDefinitionId: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User
    principalType: 'ServicePrincipal'
  }
}

// Role assignment for the current user (for local development)
module openAiRoleDev 'core/security/role.bicep' = if (!empty(principalId)) {
  scope: rg
  name: 'openai-role-dev'
  params: {
    principalId: principalId
    roleDefinitionId: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User
    principalType: 'User'
  }
}

// Outputs
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = containerRegistry.outputs.loginServer

output AZURE_OPENAI_ENDPOINT string = openAi.outputs.endpoint
output AZURE_OPENAI_DEPLOYMENT string = openAiDeploymentName
output AZURE_OPENAI_EMBEDDING_DEPLOYMENT string = 'text-embedding-ada-002'

output POSTGRES_HOST string = postgres.outputs.fqdn
output POSTGRES_DATABASE string = postgres.outputs.databaseName
output POSTGRES_URL string = postgres.outputs.connectionString
output POSTGRES_SERVER_NAME string = postgres.outputs.serverName

output MCP_SERVER_URL string = mcpServer.outputs.uri
output AGENT_URL string = agent.outputs.uri

output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.applicationInsightsConnectionString
