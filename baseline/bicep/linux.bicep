// Linux/Apache baseline (ADR-001, ADR-006): the same boring fixes as main.bicep, for Linux.
// 2-instance Ubuntu/Apache scale set with Apache logs on a data disk, an hourly retention timer,
// logrotate, a journal size cap, automatic instance repairs and a disk-free alert.
//
// Deploy main.bicep first: this reuses its network, Log Analytics workspace, action group and
// budget.
//
//   az deployment group create -g rg-fleet-health -f baseline/bicep/linux.bicep
//
// Cost: 2 x Standard_B2s Linux is roughly $2/day while running. Deallocate when not testing:
//   az vmss deallocate -g rg-fleet-health -n vmss-fleet-lx

param location string = resourceGroup().location
param prefix string = 'fleet'
param instanceCount int = 2
param vmSize string = 'Standard_B2s'
param dataDiskSizeGb int = 8
param retentionDays int = 7

@description('Alert when / or /data is more than this percent used.')
param usedSpaceAlertPercent int = 85

param adminUsername string = 'fleetadmin'

@description('SSH public key. Nobody needs to log in (Run Command), but Azure requires one.')
param sshPublicKey string

var vmssName = 'vmss-${prefix}-lx'
var cloudInit = replace(
  replace(
    loadTextContent('cloud-init.yaml'),
    '__RETENTION_SCRIPT_B64__',
    base64(loadTextContent('../log_retention.sh'))
  ),
  '__RETENTION_DAYS__',
  string(retentionDays)
)

// ---------- Shared resources from main.bicep ----------

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: 'vnet-${prefix}'
}

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: 'log-${prefix}'
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' existing = {
  name: 'ag-${prefix}'
}

// ---------- Monitoring: Linux counters and syslog via Azure Monitor Agent ----------

resource dcr 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: 'dcr-${prefix}-linux'
  location: location
  kind: 'Linux'
  properties: {
    dataSources: {
      performanceCounters: [
        {
          name: 'fleetPerfLinux'
          streams: ['Microsoft-Perf']
          samplingFrequencyInSeconds: 60
          counterSpecifiers: [
            'Logical Disk(*)\\% Used Space'
            'Logical Disk(*)\\Free Megabytes'
            'Memory(*)\\Available MBytes Memory'
            'Processor(*)\\% Processor Time'
          ]
        }
      ]
      syslog: [
        {
          name: 'fleetSyslog'
          streams: ['Microsoft-Syslog']
          facilityNames: ['kern', 'daemon', 'syslog']
          logLevels: ['Warning', 'Error', 'Critical', 'Alert', 'Emergency']
        }
      ]
    }
    destinations: {
      logAnalytics: [{ name: 'law', workspaceResourceId: law.id }]
    }
    dataFlows: [{ streams: ['Microsoft-Perf', 'Microsoft-Syslog'], destinations: ['law'] }]
  }
}

// ---------- Scale set ----------

resource vmss 'Microsoft.Compute/virtualMachineScaleSets@2024-07-01' = {
  name: vmssName
  location: location
  sku: { name: vmSize, tier: 'Standard', capacity: instanceCount }
  identity: { type: 'SystemAssigned' }
  properties: {
    orchestrationMode: 'Uniform'
    overprovision: false
    upgradePolicy: { mode: 'Automatic' }
    automaticRepairsPolicy: {
      enabled: true
      gracePeriod: 'PT20M'
    }
    virtualMachineProfile: {
      securityProfile: {
        securityType: 'TrustedLaunch'
        uefiSettings: { secureBootEnabled: true, vTpmEnabled: true }
      }
      storageProfile: {
        imageReference: {
          publisher: 'Canonical'
          offer: 'ubuntu-24_04-lts'
          sku: 'server'
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
        computerNamePrefix: '${prefix}lx'
        adminUsername: adminUsername
        customData: base64(cloudInit)
        linuxConfiguration: {
          disablePasswordAuthentication: true
          ssh: {
            publicKeys: [
              { path: '/home/${adminUsername}/.ssh/authorized_keys', keyData: sshPublicKey }
            ]
          }
        }
      }
      networkProfile: {
        networkInterfaceConfigurations: [
          {
            name: 'nic-${prefix}-lx'
            properties: {
              primary: true
              ipConfigurations: [
                {
                  name: 'ipconfig1'
                  properties: { subnet: { id: '${vnet.id}/subnets/snet-vmss' } }
                }
              ]
            }
          }
        ]
      }
      diagnosticsProfile: { bootDiagnostics: { enabled: true } }
      extensionProfile: {
        extensions: [
          {
            // Health signal for automatic instance repairs: Apache must answer on port 80.
            name: 'health'
            properties: {
              publisher: 'Microsoft.ManagedServices'
              type: 'ApplicationHealthLinux'
              typeHandlerVersion: '1.0'
              autoUpgradeMinorVersion: true
              settings: { protocol: 'http', port: 80, requestPath: '/' }
            }
          }
          {
            name: 'AzureMonitorLinuxAgent'
            properties: {
              publisher: 'Microsoft.Azure.Monitor'
              type: 'AzureMonitorLinuxAgent'
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
  name: 'dcra-${prefix}-linux'
  scope: vmss
  properties: { dataCollectionRuleId: dcr.id }
}

resource diskAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'alert-${prefix}-linux-disk-used'
  location: location
  properties: {
    displayName: 'Fleet Linux: disk used above ${usedSpaceAlertPercent}%'
    description: 'Baseline disk alert for Linux instances (ADR-001). Fires per instance and mount.'
    severity: 2
    enabled: true
    scopes: [law.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      allOf: [
        {
          query: 'Perf | where ObjectName == "Logical Disk" and CounterName == "% Used Space" and InstanceName in ("/", "/data") | summarize UsedSpace = max(CounterValue) by Computer, InstanceName | where UsedSpace > ${usedSpaceAlertPercent}'
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

output vmssName string = vmss.name
