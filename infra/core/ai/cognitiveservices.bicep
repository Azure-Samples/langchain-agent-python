metadata description = 'Creates an Azure Cognitive Services account.'
param name string
param location string = resourceGroup().location
param tags object = {}
param sku object = {
  name: 'S0'
}
param kind string = 'OpenAI'
param deployments array = []

resource cognitiveServices 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: name
  location: location
  tags: tags
  kind: kind
  sku: sku
  properties: {
    customSubDomainName: name
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
  }
}

@batchSize(1)
resource deployment 'Microsoft.CognitiveServices/accounts/deployments@2023-05-01' = [for deployment in deployments: {
  parent: cognitiveServices
  name: deployment.name
  sku: deployment.sku
  properties: {
    model: deployment.model
  }
}]

output id string = cognitiveServices.id
output name string = cognitiveServices.name
output endpoint string = cognitiveServices.properties.endpoint
