// VNet + subnet + NSG. The NSG opens SIP and the RTP UDP range inbound so the
// hostNetwork FreeSWITCH pod (on a node-public-IP node) is reachable from callers/SIPp.

@description('Resource name prefix')
param prefix string

@description('Location')
param location string

@description('RTP UDP port range start')
param rtpPortStart int = 16384

@description('RTP UDP port range end')
param rtpPortEnd int = 32768

@description('Address space for the VNet')
param vnetCidr string = '10.42.0.0/16'

@description('Subnet used by all AKS node pools')
param subnetCidr string = '10.42.0.0/20'

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-01-01' = {
  name: '${prefix}-nsg'
  location: location
  properties: {
    securityRules: [
      {
        name: 'allow-sip'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '5060'
        }
      }
      {
        name: 'allow-rtp'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Udp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '${rtpPortStart}-${rtpPortEnd}'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: '${prefix}-vnet'
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [vnetCidr]
    }
    subnets: [
      {
        name: 'nodes'
        properties: {
          addressPrefix: subnetCidr
          networkSecurityGroup: {
            id: nsg.id
          }
        }
      }
    ]
  }
}

output subnetId string = vnet.properties.subnets[0].id
