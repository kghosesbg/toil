# Copyright (C) 2015 UCSC Computational Genomics Lab
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import time
from boto.ec2.blockdevicemapping import BlockDeviceMapping, BlockDeviceType
from boto.exception import BotoServerError, EC2ResponseError
from cgcloud.lib.ec2 import ec2_instance_types
from itertools import islice

from toil.batchSystems.abstractBatchSystem import AbstractScalableBatchSystem
from toil.provisioners.abstractProvisioner import AbstractProvisioner, Shape
from toil.provisioners.cgcloud.provisioner import CGCloudProvisioner
from toil.provisioners.aws import AWSUserData
from cgcloud.lib.context import Context
from boto.utils import get_instance_metadata


coreOSAMI = 'ami-14589274'
ec2_full_policy = dict( Version="2012-10-17", Statement=[
    dict( Effect="Allow", Resource="*", Action="ec2:*" ) ] )

s3_full_policy = dict( Version="2012-10-17", Statement=[
    dict( Effect="Allow", Resource="*", Action="s3:*" ) ] )

sdb_full_policy = dict( Version="2012-10-17", Statement=[
    dict( Effect="Allow", Resource="*", Action="sdb:*" ) ] )


class AWSProvisioner(AbstractProvisioner):

    def __init__(self, config, batchSystem):
        # Do we have to delete instance profiles? What about security group?
        self.ctx = Context(availability_zone='us-west-2a', namespace='/')
        self.securityGroupName = get_instance_metadata()['security-groups'][0]
        self.instanceType = ec2_instance_types[config.nodeType]
        self.batchSystem = batchSystem

    def setNodeCount(self, numNodes, preemptable=False, force=False):

        # get all nodes in cluster
        instances = self._getAllNodesInCluster()
        # get security group
        instancesToLaunch = numNodes- len(instances)
        if instancesToLaunch > 0:
            # determine number of ephemeral drives via cgcloud-lib
            bdtKeys = ['/dev/xvdc', '/dev/xvdd']
            bdmDict = {}
            for disk in range(2, self.instanceType.disks):
                bdmDict[bdtKeys[disk-2]] = BlockDeviceType(ephemeral_name='ephemeral{}'.format(disk - 1))

            bdm = BlockDeviceMapping()
            for key, value in bdmDict.items():
                bdm[key] = value

            id = str(time.time())
            arn = self.getProfileARN(self.ctx, instanceID=id)
            # get key name
            self.ctx.ec2.run_instances(image_id=coreOSAMI, min_count=instancesToLaunch, max_count= instancesToLaunch,
                                       security_groups=[self.securityGroupName], instance_type=self.instanceType.name,
                                       instance_profile_arn=arn, user_data=AWSUserData)

        elif instancesToLaunch < 0:
            self._removeNodes(instances=instances, numNodes=numNodes, preemptable=preemptable, force=force)
        else:
            # Do nothing! Yay!
            pass
        pass

    def getNodeShape(self, preemptable=False):
        pass

    def _getAllNodesInCluster(self):
        # use security group!!!
        return self.ctx.ec2.get_only_instances(filters={
            'tag:leader_instance_id': self._instanceId, # instead do AMI ID? > Our launch time?
            'instance-state-name': 'running'})

    @staticmethod
    def launchLeaderInstance(instanceType, keyName):
        nameSpace = '/'+keyName.split('@')[0]+'/'
        ctx = Context(availability_zone='us-west-2a', namespace=nameSpace)
        profileARN = AWSProvisioner.getProfileARN(ctx, instanceID='leader')
        securityName = AWSProvisioner.getSecurityGroupName(ctx, keyName)

        ctx.ec2.run_instances(image_id=coreOSAMI, security_groups=[securityName], instance_type=instanceType,
                              instance_profile_arn=profileARN, key_name=keyName, user_data=AWSUserData)

    @staticmethod
    def getSecurityGroupName(ctx, keyName):
        name = 'toil-appliance-group-'+keyName # fixme: should be uuid
        # security group create/get. standard + all ports open within the group
        try:
            web = ctx.ec2.create_security_group(name, 'Toil appliance security group')
            # open port 22 for ssh-ing
            web.authorize(ip_protocol='tcp', from_port=22, to_port=22, cidr_ip='0.0.0.0/0')
            # the following authorizes all port access within the web security group
            web.authorize(ip_protocol='tcp', from_port=0, to_port=9000, src_group=web)
        except EC2ResponseError:
            web = ctx.ec2.get_all_security_groups(groupnames=[name])[0]
        return name

    @staticmethod
    def getProfileARN(ctx, instanceID):
        roleName='toil-leader-role'
        awsInstanceProfileName = roleName+instanceID
        policy = {}
        policy.update( dict(
            toil_iam_pass_role=dict(
                Version="2012-10-17",
                Statement=[
                    dict( Effect="Allow", Resource="*", Action="iam:PassRole" ) ] ),
            ec2_full=ec2_full_policy,
            s3_full=s3_full_policy,
            sbd_full=sdb_full_policy,
            ec2_toil_box=dict( Version="2012-10-17", Statement=[
            dict( Effect="Allow", Resource="*", Action="ec2:CreateTags" ),
            dict( Effect="Allow", Resource="*", Action="ec2:CreateVolume" ),
            dict( Effect="Allow", Resource="*", Action="ec2:AttachVolume" ) ] ) ) )

        profileName = ctx.setup_iam_ec2_role(role_name=roleName, policies=policy)
        try:
            profile = ctx.iam.get_instance_profile(awsInstanceProfileName)
        except BotoServerError as e:
            if e.status == 404:
                profile = ctx.iam.create_instance_profile( awsInstanceProfileName )
                profile = profile.create_instance_profile_response.create_instance_profile_result
            else:
                raise
        else:
            profile = profile.get_instance_profile_response.get_instance_profile_result
        profile = profile.instance_profile
        profile_arn = profile.arn

        if len( profile.roles ) > 1:
                raise RuntimeError( 'Did not expect profile to contain more than one role' )
        elif len( profile.roles ) == 1:
            # this should be profile.roles[0].role_name
            if profile.roles.member.role_name == roleName:
                return profile_arn
            else:
                ctx.iam.remove_role_from_instance_profile( awsInstanceProfileName,
                                                                profile.roles.member.role_name )
        ctx.iam.add_role_to_instance_profile( awsInstanceProfileName, roleName )
        return profile_arn

    def _removeNodes(self, instances, numNodes, preemptable=False, force=False):
        # If the batch system is scalable, we can use the number of currently running workers on
        # each node as the primary criterion to select which nodes to terminate.
        if isinstance(self.batchSystem, AbstractScalableBatchSystem):
            nodes = self.batchSystem.getNodes(preemptable)
            # Join nodes and instances on private IP address.
            nodes = [(instance, nodes.get(instance.private_ip_address)) for instance in instances]
            # Unless forced, exclude nodes with runnning workers. Note that it is possible for
            # the batch system to report stale nodes for which the corresponding instance was
            # terminated already. There can also be instances that the batch system doesn't have
            # nodes for yet. We'll ignore those, too, unless forced.
            nodes = [(instance, nodeInfo)
                     for instance, nodeInfo in nodes
                     if force or nodeInfo is not None and nodeInfo.workers < 1]
            # Sort nodes by number of workers and time left in billing cycle
            nodes.sort(key=lambda (instance, nodeInfo): (
                nodeInfo.workers if nodeInfo else 1,
                CGCloudProvisioner._remainingBillingInterval(instance)))
            nodes = nodes[:numNodes]
            # if log.isEnabledFor(logging.DEBUG):
            #     for instance, nodeInfo in nodes:
            #         log.debug("Instance %s is about to be terminated. Its node info is %r. It "
            #                   "would be billed again in %s minutes.", instance.id, nodeInfo,
            #                   60 * CGCloudProvisioner._remainingBillingInterval(instance))
            instanceIds = [instance.id for instance, nodeInfo in nodes]
        else:
            # Without load info all we can do is sort instances by time left in billing cycle.
            instances = sorted(instances,
                               key=lambda instance: (CGCloudProvisioner._remainingBillingInterval(instance)))
            instanceIds = [instance.id for instance in islice(instances, numNodes)]
        # log.info('Terminating %i instance(s).', len(instanceIds))
        if instanceIds:
            self.ctx.ec2.terminate_instances(instance_ids=instanceIds)
        return len(instanceIds)