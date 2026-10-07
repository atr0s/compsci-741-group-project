// Postgres Flexible Server with BOTH Entra and password authentication.
//
// Both, not Entra-only: the MCP server connects as the store_manager role with a
// password, and RLS is keyed on that role. infra/postgres.bicep sets EntraOnly,
// so the running environment had password auth switched on by hand afterwards -
// which is why re-running that template would break the MCP server. Declaring
// the real state here is what makes this template safe to re-run.

param name string
param location string
param tags object = {}

param adminLogin string
@secure()
param adminPassword string

@description('Public IPs to allow, for running the db/data phases from a workstation.')
param clientIpAddresses array = []

param entraAdminObjectId string
param entraAdminPrincipalName string
@allowed([
  'User'
  'ServicePrincipal'
])
param entraAdminPrincipalType string

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    version: '17'
    administratorLogin: adminLogin
    administratorLoginPassword: adminPassword
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Enabled'
      tenantId: subscription().tenantId
    }
    storage: {
      storageSizeGB: 32
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
  }
}

// 0.0.0.0 is Azure's sentinel for "any Azure service", which is how the App
// Service apps reach the server. The old template also opened 0.0.0.0-255.255.255.255
// for its post-provision script; the workstation IP below replaces that.
resource allowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2025-08-01' = {
  parent: server
  name: 'allow-azure-services'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

@batchSize(1)
resource allowClients 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2025-08-01' = [for ip in clientIpAddresses: {
  parent: server
  name: 'client-${replace(ip, '.', '-')}'
  properties: {
    startIpAddress: ip
    endIpAddress: ip
  }
}]

// Created after the firewall rules: the administrators API needs the server
// fully settled, and concurrent child updates on a new server conflict
// (same ordering as infra/postgres.bicep, where it was found the hard way).
resource entraAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2025-08-01' = {
  parent: server
  name: entraAdminObjectId
  properties: {
    tenantId: subscription().tenantId
    principalType: entraAdminPrincipalType
    principalName: entraAdminPrincipalName
  }
  dependsOn: [
    allowAzure
    allowClients
  ]
}

// Allow-list only; the extensions themselves are created by the db phase.
// Last, for https://github.com/Azure/bicep-types-az/issues/1507.
resource extensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2025-08-01' = {
  parent: server
  name: 'azure.extensions'
  properties: {
    value: 'vector,azure_ai,pg_diskann'
    source: 'user-override'
  }
  dependsOn: [
    entraAdmin
  ]
}

output name string = server.name
output fqdn string = server.properties.fullyQualifiedDomainName
output principalId string = server.identity.principalId
