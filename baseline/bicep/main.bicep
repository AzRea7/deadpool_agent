// Phase 0 baseline (ADR-001): the boring fixes, no agent.
// 2-instance Windows/IIS scale set with IIS logs on a data disk, an hourly log-retention task,
// automatic instance repairs, a disk-free alert and a monthly budget.
//
//   az deployment group create -g rg-fleet-health -f baseline/bicep/main.bicep \
//     -p alertEmail=<you@example.com>
//
// Cost: 2 x Standard_B2s Windows is roughly $4-5/day while running. Deallocate when not testing:
//   az vmss deallocate -g rg-fleet-health -n vmss-fleet

@description('Email for the disk-free alert and the budget notifications.')
param alertEmail string

param location string = resourceGroup().location
param prefix string = 'fleet'
param instanceCount int = 2
param vmSize string = 'Standard_B2s'
param dataDiskSizeGb int = 8
param retentionDays int = 7

@description('Alert when free space on C: or F: drops below this percent.')
param freeSpaceAlertPercent int = 15

param monthlyBudgetUsd int = 30

@description('First day of the current month, ISO 8601. Budgets must start on the 1st.')
param budgetStartDate string = '${utcNow('yyyy-MM')}-01T00:00:00Z'

param adminUsername string = 'fleetadmin'

@secure()
@description('Nobody needs this: instances are managed with Run Command, not RDP. Random by default.')
param adminPassword string = 'Fh1!${toUpper(take(newGuid(), 8))}${newGuid()}'

var vmssName = 'vmss-${prefix}'
var setupScript = replace(
  replace(loadTextContent('setup.ps1'), '__RETENTION_SCRIPT__', loadTextContent('../log_retention.ps1')),
  '__RETENTION_DAYS__',
  string(retentionDays)
)

// ---------- Network: private subnet, no inbound from the internet ----------

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-${prefix}'
  location: location
  properties: { securityRules: [] } // defaults: deny all inbound from the internet
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${prefix}'
  location: location
  properties: {
    addressSpace: { addressPrefixes: ['10.20.0.0/16'] }
    subnets: [
      {
        name: 'snet-vmss'
        properties: {
          addressPrefix: '10.20.1.0/24'
          networkSecurityGroup: { id: nsg.id }
          // Test env only: outbound for the monitor agent without paying for a NAT gateway.
          defaultOutboundAccess: true
        }
      }
    ]
  }
}

// ---------- Monitoring: Log Analytics + disk counters via Azure Monitor Agent ----------

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${prefix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: json('0.5') } // hard cost cap
  }
}

resource dcr 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: 'dcr-${prefix}-perf'
  location: location
  kind: 'Windows'
  properties: {
    dataSources: {
      performanceCounters: [
        {
          name: 'fleetPerf'
          streams: ['Microsoft-Perf']
          samplingFrequencyInSeconds: 60
          counterSpecifiers: [
            '\\LogicalDisk(*)\\% Free Space'
            '\\LogicalDisk(*)\\Free Megabytes'
            '\\Memory\\Available MBytes'
            '\\Processor(_Total)\\% Processor Time'
          ]
        }
      ]
    }
    destinations: {
      logAnalytics: [{ name: 'law', workspaceResourceId: law.id }]
    }
    dataFlows: [{ streams: ['Microsoft-Perf'], destinations: ['law'] }]
  }
}

// ---------- Scale set ----------

resource vmss 'Microsoft.Compute/virtualMachineScaleSets@2024-07-01' = {
  name: vmssName
  location: location
  sku: { name: vmSize, tier: 'Standard', capacity: instanceCount }
  identity: { type: 'SystemAssigned' } // for the Azure Monitor Agent
  properties: {
    orchestrationMode: 'Uniform'
    overprovision: false
    upgradePolicy: { mode: 'Automatic' }
    automaticRepairsPolicy: {
      enabled: true
      gracePeriod: 'PT30M' // IIS install + first health probe fit inside this
    }
    virtualMachineProfile: {
      securityProfile: {
        securityType: 'TrustedLaunch'
        uefiSettings: { secureBootEnabled: true, vTpmEnabled: true }
      }
      storageProfile: {
        imageReference: {
          publisher: 'MicrosoftWindowsServer'
          offer: 'WindowsServer'
          sku: '2022-datacenter-smalldisk-g2'
          version: 'latest'
        }
        osDisk: {
          createOption: 'FromImage'
          caching: 'ReadWrite'
          managedDisk: { storageAccountType: 'StandardSSD_LRS' }
        }
        dataDisks: [
          {
            lun: 0
            createOption: 'Empty'
            diskSizeGB: dataDiskSizeGb
            caching: 'None'
            managedDisk: { storageAccountType: 'StandardSSD_LRS' }
          }
        ]
      }
      osProfile: {
        computerNamePrefix: prefix
        adminUsername: adminUsername
        adminPassword: adminPassword
        customData: base64(setupScript) // lands at C:\AzureData\CustomData.bin
        windowsConfiguration: { provisionVMAgent: true, enableAutomaticUpdates: true }
      }
      networkProfile: {
        networkInterfaceConfigurations: [
          {
            name: 'nic-${prefix}'
            properties: {
              primary: true
              ipConfigurations: [
                {
                  name: 'ipconfig1'
                  properties: { subnet: { id: vnet.properties.subnets[0].id } }
                }
              ]
            }
          }
        ]
      }
      diagnosticsProfile: { bootDiagnostics: { enabled: true } } // managed storage
      extensionProfile: {
        extensions: [
          {
            name: 'setup'
            properties: {
              publisher: 'Microsoft.Compute'
              type: 'CustomScriptExtension'
              typeHandlerVersion: '1.10'
              autoUpgradeMinorVersion: true
              forceUpdateTag: uniqueString(setupScript) // rerun when the script changes
              protectedSettings: {
                commandToExecute: 'powershell -NoProfile -ExecutionPolicy Bypass -Command "New-Item -ItemType Directory -Force C:\\fleet | Out-Null; Copy-Item C:\\AzureData\\CustomData.bin C:\\fleet\\setup.ps1 -Force; & C:\\fleet\\setup.ps1"'
              }
            }
          }
          {
            // Health signal for automatic instance repairs: IIS must answer on port 80.
            name: 'health'
            properties: {
              publisher: 'Microsoft.ManagedServices'
              type: 'ApplicationHealthWindows'
              typeHandlerVersion: '1.0'
              autoUpgradeMinorVersion: true
              provisionAfterExtensions: ['setup']
              settings: { protocol: 'http', port: 80, requestPath: '/' }
            }
          }
          {
            name: 'AzureMonitorWindowsAgent'
            properties: {
              publisher: 'Microsoft.Azure.Monitor'
              type: 'AzureMonitorWindowsAgent'
              typeHandlerVersion: '1.0'
              autoUpgradeMinorVersion: true
              enableAutomaticUpgrade: true
            }
          }
        ]
      }
    }
  }
}

resource dcrAssociation 'Microsoft.Insights/dataCollectionRuleAssociations@2023-03-11' = {
  name: 'dcra-${prefix}-perf'
  scope: vmss
  properties: { dataCollectionRuleId: dcr.id }
}

// ---------- Alerts and budget ----------

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-${prefix}'
  location: 'global'
  properties: {
    groupShortName: 'fleetops'
    enabled: true
    emailReceivers: [
      { name: 'owner', emailAddress: alertEmail, useCommonAlertSchema: true }
    ]
  }
}

resource diskFreeAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'alert-${prefix}-disk-free'
  location: location
  properties: {
    displayName: 'Fleet: disk free below ${freeSpaceAlertPercent}%'
    description: 'Baseline disk-free alert (ADR-001). Fires per instance and drive.'
    severity: 2
    enabled: true
    scopes: [law.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      allOf: [
        {
          query: 'Perf | where ObjectName == "LogicalDisk" and CounterName == "% Free Space" and InstanceName in ("C:", "F:") | summarize FreeSpace = min(CounterValue) by Computer, InstanceName | where FreeSpace < ${freeSpaceAlertPercent}'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          dimensions: [
            { name: 'Computer', operator: 'Include', values: ['*'] }
            { name: 'InstanceName', operator: 'Include', values: ['*'] }
          ]
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    autoMitigate: true
    actions: { actionGroups: [actionGroup.id] }
  }
}

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'budget-${prefix}'
  properties: {
    category: 'Cost'
    amount: monthlyBudgetUsd
    timeGrain: 'Monthly'
    timePeriod: { startDate: budgetStartDate }
    notifications: {
      actual80: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [alertEmail]
      }
      forecast100: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: [alertEmail]
      }
    }
  }
}

output vmssName string = vmss.name
output workspaceId string = law.properties.customerId
