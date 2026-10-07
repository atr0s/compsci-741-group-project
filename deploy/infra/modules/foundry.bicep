// Foundry account, its project, and the model deployments the solution runs on.

param accountName string
param projectName string
param location string
param tags object = {}

@description('Array of { name, format, version, skuName, capacity }.')
param models array

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      virtualNetworkRules: []
      ipRules: []
    }
    // Key auth stays available for the optional SQL demos: Postgres' azure_ai
    // extension did not work with managed identity when the workshop was written
    // (see infra/foundry.bicep). Nothing in the deployed apps uses a key.
    disableLocalAuth: false
    defaultProject: projectName
    associatedProjects: [
      projectName
    ]
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Zava DIY'
    description: 'Zava DIY agents, published by deploy.sh.'
  }
}

// One at a time: parallel deployments on the same account are rejected.
@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = [for model in models: {
  parent: account
  name: model.name
  tags: tags
  sku: {
    name: model.skuName
    capacity: model.capacity
  }
  properties: {
    model: {
      name: model.name
      format: model.format
      version: model.version
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
  dependsOn: [
    project
  ]
}]

output accountName string = account.name
output projectName string = project.name
output projectEndpoint string = '${account.properties.endpoints['AI Foundry API']}api/projects/${project.name}'
output openAIEndpoint string = 'https://${account.name}.openai.azure.com'
