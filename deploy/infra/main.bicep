// Zava DIY solution - the whole Azure footprint in one subscription-scope deployment.
//
// Everything that is infrastructure lives here, so re-running this template is an
// idempotent UPDATE of an environment, never a partial one. What is deliberately
// NOT here, and why, is in deploy/README.md: app code (zip deploy), the database
// contents (SQL + the data restore) and the Foundry agents (data-plane objects
// with no ARM definition). deploy.sh runs those as separate phases.
//
// Independent of infra/ at the repo root: nothing here references or modifies
// that deployment, so both can exist side by side in one subscription.

targetScope = 'subscription'

@description('Short environment name. Becomes part of every resource name, so keep it to lowercase letters, digits and hyphens.')
@minLength(3)
@maxLength(14)
param environmentName string

@description('Region for every resource.')
param location string

@description('Four lowercase characters that make the globally-unique names unique. Generated once by deploy.sh init and reused, so a re-run updates rather than duplicates.')
@minLength(4)
@maxLength(4)
param suffix string

@description('Tags applied to every resource.')
param tags object = {}

@description('Model deployments on the Foundry account. The chat model is what the agents run on; the embedding model serves the MCP server\'s query embeddings.')
param models array = [
  {
    name: 'gpt-5-mini'
    format: 'OpenAI'
    version: '2025-08-07'
    skuName: 'GlobalStandard'
    capacity: 250
  }
  {
    name: 'text-embedding-3-small'
    format: 'OpenAI'
    version: '1'
    skuName: 'GlobalStandard'
    capacity: 120
  }
]

@description('Deployment name the agents are published against. Must be one of the models above.')
param chatModelDeploymentName string = 'gpt-5-mini'

@description('Deployment name the MCP server embeds queries with. Must be one of the models above.')
param embeddingModelDeploymentName string = 'text-embedding-3-small'

@description('''
App Service plan SKU, shared by both apps. B1 is the default because deploys on
F1 are fragile (no Always On, throttled CPU, SCM restarts mid-deploy). Re-run
with F1 to scale down once deployed; Always On is switched off automatically.
''')
@allowed([
  'F1'
  'B1'
  'B2'
])
param appServiceSku string = 'B1'

@description('Python runtime for both apps, as the linuxFxVersion suffix.')
param pythonVersion string = '3.14'

@description('Postgres server admin login. Only needed because password auth is on; the scripts administer the server as the Entra admin.')
param postgresAdminLogin string = 'zavaadmin'

@description('Password for postgresAdminLogin.')
@secure()
param postgresAdminPassword string

@description('Password of the store_manager role the MCP server connects as. The db phase sets the role to this same value.')
@secure()
param storeManagerPassword string

@description('Shared secret the MCP server requires in x-api-key. Also handed to the agents per turn by the chat app.')
@secure()
param mcpApiKey string

@description('Chat UI sign-ins: name:password:store|store, comma-separated. See src/python/web_app/stores.py for store keys.')
@secure()
param webAuthUsers string

@description('Public IPs allowed through the Postgres firewall, for running the db and data phases from a workstation.')
param clientIpAddresses array = []

@description('Principal type of whoever runs the deployment. The deployer becomes the Postgres Entra admin and gets data-plane access to Foundry.')
@allowed([
  'User'
  'ServicePrincipal'
])
param deployerPrincipalType string = 'User'

@description('Agent names the chat app addresses. They are fixed strings (scripts/provision_agent.py), so the app can be configured before the agents exist.')
param agentNameDiy string = 'zava-diy-advisor'

@description('Monthly budget for the resource group, in the billing currency. 0, or no alert emails, skips it.')
param monthlyBudget int = 60

@description('Addresses notified when the budget crosses a threshold.')
param budgetAlertEmails array = []

var base = toLower('${environmentName}-${suffix}')
var allTags = union({ solution: 'zava-diy', environment: environmentName }, tags)

// Built-in role definition ids. Cognitive Services User is used for the chat app
// and the deployer rather than Azure AI Developer: its data actions are
// Microsoft.CognitiveServices/*, which covers the Foundry agents API, whereas
// Azure AI Developer's stop at OpenAI/Speech/ContentSafety/MaaS.
var roleCognitiveServicesUser = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var roleCognitiveServicesOpenAIUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource rg 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: 'rg-${base}'
  location: location
  tags: allTags
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  scope: rg
  params: {
    name: 'appi-${base}'
    location: location
    tags: allTags
  }
}

module foundry 'modules/foundry.bicep' = {
  name: 'foundry'
  scope: rg
  params: {
    accountName: 'fdy-${base}'
    projectName: 'prj-${base}'
    location: location
    tags: allTags
    models: models
  }
}

module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  scope: rg
  params: {
    name: '${base}-postgresql'
    location: location
    tags: allTags
    adminLogin: postgresAdminLogin
    adminPassword: postgresAdminPassword
    clientIpAddresses: clientIpAddresses
    entraAdminObjectId: deployer().objectId
    entraAdminPrincipalName: deployerPrincipalType == 'User' ? deployer().userPrincipalName : deployer().objectId
    entraAdminPrincipalType: deployerPrincipalType
  }
}

module apps 'modules/appservice.bicep' = {
  name: 'appservice'
  scope: rg
  params: {
    planName: 'asp-${base}'
    mcpAppName: 'mcp-${base}'
    webAppName: 'web-${base}'
    location: location
    tags: allTags
    sku: appServiceSku
    pythonVersion: pythonVersion
    postgresFqdn: postgres.outputs.fqdn
    storeManagerPassword: storeManagerPassword
    mcpApiKey: mcpApiKey
    webAuthUsers: webAuthUsers
    azureOpenAIEndpoint: foundry.outputs.openAIEndpoint
    embeddingDeploymentName: embeddingModelDeploymentName
    projectEndpoint: foundry.outputs.projectEndpoint
    agentNameDiy: agentNameDiy
  }
}

// Every identity that talks to Foundry, scoped to the account rather than the
// resource group so nothing else in the group is reachable through these grants.
module foundryRoles 'modules/foundry-roles.bicep' = {
  name: 'foundry-roles'
  scope: rg
  params: {
    accountName: foundry.outputs.accountName
    assignments: [
      // The chat app runs agent turns: conversations, responses, agent lookup.
      { principalId: apps.outputs.webPrincipalId, principalType: 'ServicePrincipal', roleDefinitionId: roleCognitiveServicesUser }
      // The MCP server embeds queries with its own identity.
      { principalId: apps.outputs.mcpPrincipalId, principalType: 'ServicePrincipal', roleDefinitionId: roleCognitiveServicesOpenAIUser }
      // Postgres' azure_ai extension, used only by the optional SQL demos.
      { principalId: postgres.outputs.principalId, principalType: 'ServicePrincipal', roleDefinitionId: roleCognitiveServicesOpenAIUser }
      // The deployer publishes agents and runs the smoke test and evals.
      { principalId: deployer().objectId, principalType: deployerPrincipalType, roleDefinitionId: roleCognitiveServicesUser }
    ]
  }
}

module budget 'modules/budget.bicep' = if (monthlyBudget > 0 && !empty(budgetAlertEmails)) {
  name: 'budget'
  scope: rg
  params: {
    budgetName: 'budget-${base}'
    amount: monthlyBudget
    contactEmails: budgetAlertEmails
  }
}

// Outputs are written into the environment file by deploy.sh; every later phase
// reads from there, so these names are the contract with the scripts.
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_LOCATION string = location
output FOUNDRY_ACCOUNT_NAME string = foundry.outputs.accountName
output PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output AZURE_OPENAI_ENDPOINT string = foundry.outputs.openAIEndpoint
output GPT_MODEL_DEPLOYMENT_NAME string = chatModelDeploymentName
output EMBEDDING_MODEL_DEPLOYMENT_NAME string = embeddingModelDeploymentName
output POSTGRES_SERVER_NAME string = postgres.outputs.name
output POSTGRES_SERVER_FQDN string = postgres.outputs.fqdn
output POSTGRES_ENTRA_ADMIN string = deployerPrincipalType == 'User' ? deployer().userPrincipalName : deployer().objectId
output MCP_APP_NAME string = apps.outputs.mcpAppName
output MCP_SERVER_URL string = apps.outputs.mcpServerUrl
output MCP_HEALTH_URL string = apps.outputs.mcpHealthUrl
output WEB_APP_NAME string = apps.outputs.webAppName
output WEB_APP_URL string = apps.outputs.webAppUrl
output APP_SERVICE_SKU string = appServiceSku
output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.connectionString
