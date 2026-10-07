@description('Name of the budget resource.')
param budgetName string

@description('''
Monthly ceiling, in the subscription's billing currency (NZD for a New Zealand
subscription). The evaluation deployment uses 60.
''')
param amount int

@description('''
First day of the month the budget starts tracking. Azure requires the first of a
month, no more than three months in the past. Defaults to the current month.
''')
param startDate string = utcNow('yyyy-MM-01')

@description('Addresses that receive the threshold alerts. At least one is required.')
param contactEmails array

@description('''
Percentage thresholds that raise an *actual* spend alert - money already spent.
''')
param actualThresholds array = [
  50
  75
  90
  100
]

@description('''
Percentage thresholds that raise a *forecast* alert. These are the useful ones:
they fire while there is still time to act, rather than after the credit is gone.
''')
param forecastThresholds array = [
  100
]

// Azure allows at most 5 notifications per budget, and the defaults above use
// all 5. Adding a threshold means removing one.
//
// An Azure budget ALERTS, it does not cap. Nothing here will stop a resource or
// block a deployment - Azure has no such control. The application-level caps
// (row and batch limits in the MCP server) are what actually bound spend; this exists so an overrun is
// noticed within hours rather than at the end of the month.
resource budget 'Microsoft.Consumption/budgets@2023-05-01' = {
  name: budgetName
  properties: {
    category: 'Cost'
    amount: amount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: startDate
      endDate: dateTimeAdd(startDate, 'P1Y', 'yyyy-MM-dd')
    }
    notifications: reduce(
      union(
        map(actualThresholds, t => { threshold: t, kind: 'Actual' }),
        map(forecastThresholds, t => { threshold: t, kind: 'Forecasted' })
      ),
      {},
      (acc, n) => union(acc, {
        '${toLower(n.kind)}_${n.threshold}_percent': {
          enabled: true
          operator: 'GreaterThanOrEqualTo'
          threshold: n.threshold
          thresholdType: n.kind
          contactEmails: contactEmails
        }
      })
    )
  }
}

output budgetId string = budget.id
output budgetAmount int = amount
output notificationCount int = length(actualThresholds) + length(forecastThresholds)
