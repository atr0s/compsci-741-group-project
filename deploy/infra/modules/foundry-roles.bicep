// Role assignments scoped to the Foundry account.

param accountName string

@description('Array of { principalId, principalType, roleDefinitionId }.')
param assignments array

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: accountName
}

resource roleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for a in assignments: {
  name: guid(account.id, a.principalId, a.roleDefinitionId)
  scope: account
  properties: {
    principalId: a.principalId
    principalType: a.principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', a.roleDefinitionId)
  }
}]
