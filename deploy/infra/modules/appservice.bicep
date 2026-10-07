// One Linux App Service plan hosting both apps: the catalog MCP server and the
// chat frontend.
//
// Both apps belong to this template. In the running environment the chat app was
// created by hand onto the MCP template's plan, so tearing down the MCP layer
// would take the chat app with it. Owning plan and both sites together removes
// that coupling.

param planName string
param mcpAppName string
param webAppName string
param location string
param tags object = {}

@allowed([
  'F1'
  'B1'
  'B2'
])
param sku string
param pythonVersion string

param postgresFqdn string
@secure()
param storeManagerPassword string
@secure()
param mcpApiKey string
@secure()
param webAuthUsers string

param azureOpenAIEndpoint string
param embeddingDeploymentName string
param projectEndpoint string
param agentNameDiy string

// Always On is unavailable on F1. Leaving it true there makes the site fail to
// start and blocks a later scale-down, so it follows the SKU.
var alwaysOn = sku != 'F1'

// FastMCP's DNS-rebinding guard only knows localhost unless told otherwise, and
// rejects every request behind the site FQDN with "Invalid Host header".
var mcpHostName = '${mcpAppName}.azurewebsites.net'

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    name: sku
  }
  // Without reserved the plan is created as Windows and linuxFxVersion is ignored.
  properties: {
    reserved: true
  }
}

resource mcp 'Microsoft.Web/sites@2023-12-01' = {
  name: mcpAppName
  location: location
  tags: tags
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|${pythonVersion}'
      alwaysOn: alwaysOn
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      // Relative on purpose: Oryx extracts the build to a random /tmp directory
      // and runs this from there, so an absolute wwwroot path fails with exit 2.
      appCommandLine: 'python catalog/zava_catalog.py'
    }
  }
}

resource mcpSettings 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: mcp
  name: 'appsettings'
  properties: {
    POSTGRES_URL: 'postgresql://store_manager:${storeManagerPassword}@${postgresFqdn}:5432/zava?sslmode=require'
    MCP_API_KEY: mcpApiKey
    AZURE_OPENAI_ENDPOINT: azureOpenAIEndpoint
    EMBEDDING_MODEL_DEPLOYMENT_NAME: embeddingDeploymentName
    FASTMCP_HOST: '0.0.0.0'
    MCP_ALLOWED_HOSTS: mcpHostName
    SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'
    // The first cold start extracts the build before binding a port; the default
    // 230s window is not always enough.
    WEBSITES_CONTAINER_START_TIME_LIMIT: '600'
  }
}

resource web 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  tags: tags
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|${pythonVersion}'
      alwaysOn: alwaysOn
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      // The package mirrors src/: python/web_app/ and shared/static/, which is
      // what web_app.py resolves its static directory against.
      appCommandLine: 'python python/web_app/web_app.py'
    }
  }
}

resource webSettings 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: web
  name: 'appsettings'
  properties: {
    AGENT_BACKEND: 'foundry'
    PROJECT_ENDPOINT: projectEndpoint
    AGENT_NAME_DIY: agentNameDiy
    MCP_SERVER_URL: 'https://${mcpHostName}/mcp'
    MCP_API_KEY: mcpApiKey
    WEB_AUTH_USERS: webAuthUsers
    SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'
    WEBSITES_CONTAINER_START_TIME_LIMIT: '600'
  }
}

// Publishing with shared basic-auth credentials is off, matching the running
// environment: deploys authenticate to Kudu with the deployer's Entra token.
resource mcpScmBasicAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2023-12-01' = {
  parent: mcp
  name: 'scm'
  properties: {
    allow: false
  }
}

resource mcpFtpBasicAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2023-12-01' = {
  parent: mcp
  name: 'ftp'
  properties: {
    allow: false
  }
}

resource webScmBasicAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2023-12-01' = {
  parent: web
  name: 'scm'
  properties: {
    allow: false
  }
}

resource webFtpBasicAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2023-12-01' = {
  parent: web
  name: 'ftp'
  properties: {
    allow: false
  }
}

output mcpAppName string = mcp.name
output mcpPrincipalId string = mcp.identity.principalId
output mcpServerUrl string = 'https://${mcp.properties.defaultHostName}/mcp'
output mcpHealthUrl string = 'https://${mcp.properties.defaultHostName}/healthz'
output webAppName string = web.name
output webPrincipalId string = web.identity.principalId
output webAppUrl string = 'https://${web.properties.defaultHostName}'
