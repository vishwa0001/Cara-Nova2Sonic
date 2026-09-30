"""Unit and dry-run verification tests for deploy_infrastructure.py and cleanup_resources.py."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.deploy_infrastructure import (
    deploy_infrastructure,
    discover_connect_instance,
    ensure_connect_media_streaming_storage,
    ensure_dynamodb_tables,
    ensure_ecs_resources,
    ensure_kvs_stream,
    get_or_claim_phone_number,
    get_or_create_connect_instance,
    sync_contact_flow,
    sync_ssm_parameters,
)
from scripts.cleanup_resources import (
    cleanup_all_resources,
    cleanup_connect_contact_flows,
    cleanup_connect_media_streaming_storage,
    cleanup_connect_phone_numbers,
    cleanup_dynamodb_tables,
    cleanup_ecs_resources,
    cleanup_eventbridge_rules,
    cleanup_kvs_stream,
    cleanup_local_state,
    cleanup_ssm_parameters,
)


class TestDeploymentScripts(unittest.TestCase):
    def setUp(self):
        self.mock_session = MagicMock()
        self.mock_session.region_name = "us-east-1"
        self.mock_connect = MagicMock()
        self.mock_dynamo = MagicMock()
        self.mock_kvs = MagicMock()
        self.mock_ecs = MagicMock()
        self.mock_ssm = MagicMock()
        self.mock_events = MagicMock()
        self.mock_iam = MagicMock()
        self.mock_sts = MagicMock()
        self.mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
        self.mock_ec2 = MagicMock()
        self.mock_ec2.describe_subnets.return_value = {"Subnets": [{"SubnetId": "subnet-0123456789abcdef0"}]}

        def client_side_effect(service_name, **kwargs):
            if service_name == "connect":
                return self.mock_connect
            elif service_name == "dynamodb":
                return self.mock_dynamo
            elif service_name == "kinesisvideo":
                return self.mock_kvs
            elif service_name == "ecs":
                return self.mock_ecs
            elif service_name == "ssm":
                return self.mock_ssm
            elif service_name == "events":
                return self.mock_events
            elif service_name == "iam":
                return self.mock_iam
            elif service_name == "sts":
                return self.mock_sts
            elif service_name == "ec2":
                return self.mock_ec2
            return MagicMock()

        self.mock_session.client.side_effect = client_side_effect

    # ==========================================================================
    # DEPLOY INFRASTRUCTURE TESTS
    # ==========================================================================

    def test_deploy_infrastructure_dry_run_executes_zero_mutations(self):
        """Verify deploy_infrastructure --dry-run performs no create/update API mutations."""
        with patch("scripts.deploy_infrastructure.get_aws_session", return_value=self.mock_session):
            summary = deploy_infrastructure(dry_run=True)

            self.assertEqual(summary["status"], "DRY_RUN_COMPLETED")
            self.assertEqual(summary["region"], "us-east-1")
            self.assertIn("instance_id", summary["connect"])

            # Verify no mutation methods were called
            self.mock_dynamo.create_table.assert_not_called()
            self.mock_kvs.create_stream.assert_not_called()
            self.mock_ecs.create_cluster.assert_not_called()
            self.mock_ecs.create_service.assert_not_called()
            self.mock_ssm.put_parameter.assert_not_called()
            self.mock_connect.create_contact_flow.assert_not_called()

    def test_get_or_create_connect_instance_existing_by_id_returns_immediately(self):
        self.mock_connect.describe_instance.return_value = {
            "Instance": {
                "Id": "inst-1111",
                "Arn": "arn:aws:connect:us-east-1:123456789012:instance/inst-1111",
                "InstanceAlias": "Cara-Connect",
                "InstanceStatus": "ACTIVE",
            }
        }
        res = get_or_create_connect_instance(self.mock_session, instance_id="inst-1111")
        self.assertEqual(res["instance_id"], "inst-1111")
        self.assertEqual(res["instance_alias"], "Cara-Connect")
        self.assertEqual(res["status"], "ACTIVE")
        self.mock_connect.create_instance.assert_not_called()

    @patch.dict(os.environ, {"CONNECT_INSTANCE_ID": ""}, clear=False)
    def test_get_or_create_connect_instance_existing_by_alias_returns_immediately(self):
        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = [
            {
                "InstanceSummaryList": [
                    {
                        "Id": "inst-2222",
                        "Arn": "arn:aws:connect:us-east-1:123456789012:instance/inst-2222",
                        "InstanceAlias": "Cara-Connect",
                        "InstanceStatus": "ACTIVE",
                    }
                ]
            }
        ]
        self.mock_connect.get_paginator.return_value = paginator_mock

        res = get_or_create_connect_instance(self.mock_session, alias="Cara-Connect", dry_run=False)
        self.assertEqual(res["instance_id"], "inst-2222")
        self.assertEqual(res["instance_alias"], "Cara-Connect")
        self.assertEqual(res["status"], "ACTIVE")
        self.mock_connect.create_instance.assert_not_called()

    @patch("time.sleep", return_value=None)
    @patch.dict(os.environ, {"CONNECT_INSTANCE_ID": ""}, clear=False)
    def test_get_or_create_connect_instance_missing_creates_and_polls_until_active(self, mock_sleep):
        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = [{"InstanceSummaryList": []}]
        self.mock_connect.get_paginator.return_value = paginator_mock

        self.mock_connect.create_instance.return_value = {
            "Id": "inst-new-999",
            "Arn": "arn:aws:connect:us-east-1:123456789012:instance/inst-new-999",
        }
        self.mock_connect.describe_instance.return_value = {
            "Instance": {
                "Id": "inst-new-999",
                "Arn": "arn:aws:connect:us-east-1:123456789012:instance/inst-new-999",
                "InstanceAlias": "Cara-Connect",
                "InstanceStatus": "ACTIVE",
            }
        }

        res = get_or_create_connect_instance(self.mock_session, alias="Cara-Connect", dry_run=False)

        self.mock_connect.create_instance.assert_called_once_with(
            IdentityManagementType="CONNECT_MANAGED",
            InstanceAlias="Cara-Connect",
            InboundCallsEnabled=True,
            OutboundCallsEnabled=True,
            Tags={"Project": "CareHarmony-Cara", "Environment": "Development"},
        )
        self.assertEqual(res["instance_id"], "inst-new-999")
        self.assertEqual(res["status"], "ACTIVE")

    @patch.dict(os.environ, {"CONNECT_INSTANCE_ID": ""}, clear=False)
    def test_get_or_create_connect_instance_missing_dry_run_simulates(self):
        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = [{"InstanceSummaryList": []}]
        self.mock_connect.get_paginator.return_value = paginator_mock

        res = get_or_create_connect_instance(self.mock_session, alias="Cara-Connect", dry_run=True)
        self.mock_connect.create_instance.assert_not_called()
        self.assertEqual(res["instance_id"], "mock-connect-id-12345")
        self.assertEqual(res["status"], "ACTIVE")

    def test_ensure_dynamodb_tables_idempotent_existing(self):
        self.mock_dynamo.describe_table.return_value = {
            "Table": {"TableStatus": "ACTIVE"}
        }
        statuses = ensure_dynamodb_tables(self.mock_session, dry_run=False)
        self.assertEqual(statuses["CareHarmony_OutreachRecords"], "ACTIVE")
        self.assertEqual(statuses["CareHarmony_HandoffEvents"], "ACTIVE")
        self.mock_dynamo.create_table.assert_not_called()

    def test_ensure_kvs_stream_existing(self):
        self.mock_kvs.describe_stream.return_value = {
            "StreamInfo": {
                "StreamARN": "arn:aws:kinesisvideo:us-east-1:12345:stream/test/1",
                "Status": "ACTIVE",
            }
        }
        res = ensure_kvs_stream(self.mock_session, dry_run=False)
        self.assertEqual(res["status"], "ACTIVE")
        self.mock_kvs.create_stream.assert_not_called()

    def test_ensure_ecs_resources_updates_existing_service(self):
        self.mock_ecs.describe_clusters.return_value = {
            "clusters": [{"clusterArn": "arn:cluster", "status": "ACTIVE"}]
        }
        self.mock_ecs.register_task_definition.return_value = {
            "taskDefinition": {"taskDefinitionArn": "arn:task:1"}
        }
        self.mock_ecs.describe_services.return_value = {
            "services": [{"serviceArn": "arn:service", "status": "ACTIVE"}]
        }

        res = ensure_ecs_resources(self.mock_session, connect_instance_id="inst-1", dry_run=False)
        self.assertEqual(res["task_definition_arn"], "arn:task:1")
        self.mock_ecs.update_service.assert_called_once()
        # Verify executionRoleArn was passed to register_task_definition
        self.mock_ecs.register_task_definition.assert_called_once()
        call_kwargs = self.mock_ecs.register_task_definition.call_args[1]
        self.assertIn("executionRoleArn", call_kwargs)
        self.assertTrue(call_kwargs["executionRoleArn"].startswith("arn:aws:iam::"))

    def test_sync_ssm_parameters_avoids_tags_in_put_parameter(self):
        self.mock_ssm.exceptions.ParameterNotFound = Exception
        self.mock_ssm.get_parameter.side_effect = Exception("NotFound")

        res = sync_ssm_parameters(
            self.mock_session,
            {"/careharmony/test_key": "test_value"},
            dry_run=False,
        )
        self.assertEqual(res["/careharmony/test_key"], "UPDATED")
        self.mock_ssm.put_parameter.assert_called_once_with(
            Name="/careharmony/test_key",
            Value="test_value",
            Type="String",
            Overwrite=True,
        )
        self.mock_ssm.add_tags_to_resource.assert_called_once_with(
            ResourceType="Parameter",
            ResourceId="/careharmony/test_key",
            Tags=[{"Key": "Project", "Value": "CareHarmony-Cara"}],
        )

    def test_sync_contact_flow_creates_valid_json(self):
        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = [{"ContactFlowSummaryList": []}]
        self.mock_connect.get_paginator.return_value = paginator_mock
        self.mock_connect.create_contact_flow.return_value = {"ContactFlowId": "flow-abc-123"}

        res = sync_contact_flow(self.mock_session, instance_id="inst-1", dry_run=False)
        self.assertEqual(res["flow_id"], "flow-abc-123")
        self.assertEqual(res["status"], "CREATED")
        self.mock_connect.create_contact_flow.assert_called_once()
        create_args = self.mock_connect.create_contact_flow.call_args[1]
        flow_content = json.loads(create_args["Content"])
        self.assertEqual(flow_content["Version"], "2019-10-30")
        action_types = [a["Type"] for a in flow_content["Actions"]]
        self.assertIn("DisconnectParticipant", action_types)
        self.assertIn("UpdateContactMediaStreamingBehavior", action_types)

    def test_ensure_connect_media_streaming_storage_new(self):
        self.mock_connect.list_instance_storage_configs.return_value = {"StorageConfigs": []}
        self.mock_connect.associate_instance_storage_config.return_value = {
            "AssociationId": "assoc-12345"
        }

        res = ensure_connect_media_streaming_storage(
            self.mock_session,
            instance_id="inst-1",
            kvs_stream_prefix="careharmony-outreach-media-stream",
            dry_run=False,
        )
        self.assertEqual(res["status"], "ASSOCIATED")
        self.assertEqual(res["association_id"], "assoc-12345")
        self.mock_connect.associate_instance_storage_config.assert_called_once()

    def test_ensure_connect_media_streaming_storage_existing(self):
        self.mock_connect.list_instance_storage_configs.return_value = {
            "StorageConfigs": [{"AssociationId": "existing-assoc-999"}]
        }

        res = ensure_connect_media_streaming_storage(
            self.mock_session,
            instance_id="inst-1",
            dry_run=False,
        )
        self.assertEqual(res["status"], "ALREADY_ASSOCIATED")
        self.assertEqual(res["association_id"], "existing-assoc-999")
        self.mock_connect.associate_instance_storage_config.assert_not_called()

    def test_cleanup_connect_media_streaming_storage(self):
        self.mock_connect.list_instance_storage_configs.return_value = {
            "StorageConfigs": [{"AssociationId": "assoc-to-delete"}]
        }

        res = cleanup_connect_media_streaming_storage(
            self.mock_session,
            instance_id="inst-1",
            dry_run=False,
        )
        self.assertEqual(res["assoc-to-delete"], "DISASSOCIATED")
        self.mock_connect.disassociate_instance_storage_config.assert_called_once_with(
            InstanceId="inst-1",
            AssociationId="assoc-to-delete",
            ResourceType="MEDIA_STREAMS",
        )

    def test_get_or_claim_phone_number_existing_bound(self):
        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = [
            {
                "PhoneNumberSummaryList": [
                    {
                        "Id": "phone-1",
                        "Arn": "arn:aws:connect:us-east-1:123456789012:phone-number/phone-1",
                        "PhoneNumber": "+12065550100",
                    }
                ]
            }
        ]
        self.mock_connect.get_paginator.return_value = paginator_mock
        self.mock_connect.describe_phone_number.return_value = {
            "ClaimedPhoneNumberSummary": {
                "PhoneNumber": "+12065550100",
                "TargetArn": "arn:aws:connect:us-east-1:123456789012:instance/inst-1/contact-flow/flow-123",
            }
        }

        res = get_or_claim_phone_number(
            self.mock_session,
            instance_id="inst-1",
            instance_arn="arn:inst-1",
            contact_flow_id="flow-123",
            dry_run=False,
        )
        self.assertEqual(res["number"], "+12065550100")
        self.assertEqual(res["id"], "phone-1")
        self.assertEqual(res["status"], "ASSOCIATED")
        self.mock_connect.update_phone_number_metadata.assert_not_called()
        self.mock_connect.claim_phone_number.assert_not_called()

    def test_get_or_claim_phone_number_existing_rebind(self):
        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = [
            {
                "PhoneNumberSummaryList": [
                    {
                        "Id": "phone-2",
                        "Arn": "arn:aws:connect:us-east-1:123456789012:phone-number/phone-2",
                        "PhoneNumber": "+12065550101",
                    }
                ]
            }
        ]
        self.mock_connect.get_paginator.return_value = paginator_mock
        self.mock_connect.describe_phone_number.return_value = {
            "ClaimedPhoneNumberSummary": {
                "PhoneNumber": "+12065550101",
                "TargetArn": "arn:aws:connect:us-east-1:123456789012:instance/inst-1/contact-flow/old-flow",
            }
        }

        res = get_or_claim_phone_number(
            self.mock_session,
            instance_id="inst-1",
            instance_arn="arn:inst-1",
            contact_flow_id="flow-new-999",
            dry_run=False,
        )
        self.assertEqual(res["number"], "+12065550101")
        self.assertEqual(res["status"], "ASSOCIATED")
        self.mock_connect.associate_phone_number_contact_flow.assert_called_once_with(
            PhoneNumberId="phone-2",
            InstanceId="inst-1",
            ContactFlowId="flow-new-999",
        )

    def test_get_or_claim_phone_number_claim_new(self):
        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = [{"PhoneNumberSummaryList": []}]
        self.mock_connect.get_paginator.return_value = paginator_mock

        self.mock_connect.search_available_phone_numbers.return_value = {
            "AvailableNumbersList": [{"PhoneNumber": "+12065550199"}]
        }
        self.mock_connect.claim_phone_number.return_value = {
            "PhoneNumberId": "phone-claimed-999",
            "PhoneNumberArn": "arn:aws:connect:us-east-1:123456789012:phone-number/phone-claimed-999",
        }

        res = get_or_claim_phone_number(
            self.mock_session,
            instance_id="inst-1",
            instance_arn="arn:inst-1",
            contact_flow_id="flow-123",
            dry_run=False,
        )
        self.assertEqual(res["number"], "+12065550199")
        self.assertEqual(res["id"], "phone-claimed-999")
        self.assertEqual(res["status"], "CLAIMED_AND_ASSOCIATED")
        self.mock_connect.claim_phone_number.assert_called_once_with(
            TargetArn="arn:inst-1",
            PhoneNumber="+12065550199",
            PhoneNumberDescription="CareHarmony Cara AI Voice Outreach DID",
            Tags={"Project": "CareHarmony-Cara", "Environment": "Development"},
        )
        self.mock_connect.associate_phone_number_contact_flow.assert_called_once_with(
            PhoneNumberId="phone-claimed-999",
            InstanceId="inst-1",
            ContactFlowId="flow-123",
        )

    def test_get_or_claim_phone_number_dry_run(self):
        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = [{"PhoneNumberSummaryList": []}]
        self.mock_connect.get_paginator.return_value = paginator_mock

        res = get_or_claim_phone_number(
            self.mock_session,
            instance_id="inst-1",
            instance_arn="arn:inst-1",
            contact_flow_id="flow-123",
            dry_run=True,
        )
        self.assertEqual(res["number"], "+15822671755")
        self.assertEqual(res["status"], "DRY_RUN_ASSOCIATED")
        self.mock_connect.claim_phone_number.assert_not_called()

    # ==========================================================================
    # CLEANUP RESOURCES (NUCLEAR TEARDOWN) TESTS
    # ==========================================================================

    def test_cleanup_all_resources_dry_run_executes_zero_deletions(self):
        """Verify cleanup_all_resources --dry-run identifies resources and executes no deletes."""
        with patch("scripts.cleanup_resources.get_aws_session", return_value=self.mock_session):
            summary = cleanup_all_resources(dry_run=True, force=True)

            self.assertEqual(summary["status"], "DRY_RUN_COMPLETED")

            # Verify no delete methods were called
            self.mock_events.delete_rule.assert_not_called()
            self.mock_ecs.delete_service.assert_not_called()
            self.mock_ecs.delete_cluster.assert_not_called()
            self.mock_kvs.delete_stream.assert_not_called()
            self.mock_dynamo.delete_table.assert_not_called()
            self.mock_ssm.delete_parameters.assert_not_called()

    def test_cleanup_eventbridge_rules(self):
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Rules": [{"Name": "careharmony-campaign-daily-10am"}]}
        ]
        self.mock_events.get_paginator.return_value = paginator
        self.mock_events.list_targets_by_rule.return_value = {
            "Targets": [{"Id": "target-1"}]
        }

        deleted = cleanup_eventbridge_rules(self.mock_session, dry_run=False)
        self.assertEqual(deleted, ["careharmony-campaign-daily-10am"])
        self.mock_events.remove_targets.assert_called_once_with(
            Rule="careharmony-campaign-daily-10am", Ids=["target-1"]
        )
        self.mock_events.delete_rule.assert_called_once_with(
            Name="careharmony-campaign-daily-10am"
        )

    def test_cleanup_dynamodb_tables(self):
        results = cleanup_dynamodb_tables(
            self.mock_session,
            tables=["CareHarmony_OutreachRecords"],
            dry_run=False,
        )
        self.assertEqual(results["CareHarmony_OutreachRecords"], "DELETED")
        self.mock_dynamo.delete_table.assert_called_once_with(
            TableName="CareHarmony_OutreachRecords"
        )

    def test_cleanup_connect_guardrail_preserves_instance(self):
        """Confirm the Amazon Connect instance itself is strictly protected from deletion."""
        res = cleanup_connect_contact_flows(self.mock_session, dry_run=False)
        self.assertNotIn("delete_instance", dir(self.mock_connect))
        # Ensure no instance deletion was attempted
        self.mock_connect.delete_instance.assert_not_called() if hasattr(self.mock_connect, "delete_instance") else None

    def test_cleanup_phone_number_retention_vs_release(self):
        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = [
            {
                "PhoneNumberSummaryList": [
                    {"Id": "p-123", "PhoneNumber": "+12065550100"}
                ]
            }
        ]
        self.mock_connect.get_paginator.return_value = paginator_mock

        # 1. Retention test (default)
        res_retained = cleanup_connect_phone_numbers(
            self.mock_session,
            instance_id="inst-1",
            release_phone=False,
            dry_run=False,
        )
        self.assertEqual(res_retained["+12065550100"], "RETAINED")
        self.mock_connect.release_phone_number.assert_not_called()

        # 2. Release test (--release-phone)
        res_released = cleanup_connect_phone_numbers(
            self.mock_session,
            instance_id="inst-1",
            release_phone=True,
            dry_run=False,
        )
        self.assertEqual(res_released["+12065550100"], "RELEASED")
        self.mock_connect.release_phone_number.assert_called_once_with(PhoneNumberId="p-123")


if __name__ == "__main__":
    unittest.main()
