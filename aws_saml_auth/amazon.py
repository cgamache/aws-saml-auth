#!/usr/bin/env python

import base64
import logging

import boto3
import os
import re
import json

from datetime import datetime
from threading import Thread

from botocore.exceptions import ClientError, ProfileNotFound
import botocore
import botocore.client
from lxml import etree


class ExpectedAmazonException(Exception):
    def __init__(self, *args):
        super(ExpectedAmazonException, self).__init__(*args)


class Amazon:
    def __init__(self, config, saml_xml):
        self.config = config
        self.saml_xml = saml_xml
        if config.token_cache:
            self.__token = {"Credentials": config.token_cache}
            self.__roles = [config.role_arn]
        else:
            self.__token = None

    @property
    def sts_client(self):
        try:
            profile = os.environ.get("AWS_PROFILE")
            if profile is not None:
                del os.environ["AWS_PROFILE"]
            client = boto3.client("sts", region_name=self.config.region)
            if profile is not None:
                os.environ["AWS_PROFILE"] = profile
            return client
        except ProfileNotFound as ex:
            raise ExpectedAmazonException("Error : {}.".format(ex))

    @property
    def base64_encoded_saml(self):
        return base64.b64encode(self.saml_xml).decode("utf-8")

    @property
    def token(self):
        if self.__token is None:
            self.__token = self.assume_role(
                self.config.role_arn,
                self.config.provider,
                self.base64_encoded_saml,
                self.config.duration,
            )
        return self.__token

    @property
    def access_key_id(self):
        return self.token["Credentials"]["AccessKeyId"]

    @property
    def secret_access_key(self):
        return self.token["Credentials"]["SecretAccessKey"]

    @property
    def session_token(self):
        return self.token["Credentials"]["SessionToken"]

    @property
    def expiration(self):
        return self.token["Credentials"]["Expiration"]

    def print_export_line(self):
        export_template = "export AWS_ACCESS_KEY_ID='{}' AWS_SECRET_ACCESS_KEY='{}' AWS_SESSION_TOKEN='{}'"

        formatted = export_template.format(
            self.access_key_id,
            self.secret_access_key,
            self.session_token,
        )

        print(formatted)

    def print_credential_process(self):
        print(
            json.dumps(
                {
                    "Version": 1,
                    "AccessKeyId": self.access_key_id,
                    "SecretAccessKey": self.secret_access_key,
                    "SessionToken": self.session_token,
                    "Expiration": self.expiration.isoformat(),
                }
            )
        )

    @property
    def roles(self):
        assert self.saml_xml is not None, "Cannot load roles without saml."
        doc = etree.fromstring(self.saml_xml)
        roles = {}
        for x in doc.xpath(
            '//*[@Name = "https://aws.amazon.com/SAML/Attributes/Role"]//text()'
        ):
            if "arn:aws:iam:" in x or "arn:aws-us-gov:iam:" in x:
                res = x.split(",")
                roles[res[0]] = res[1]
        return roles

    def assume_role(
        self, role, principal, saml_assertion, duration=None, auto_duration=True
    ):
        sts_call_vars = {
            "RoleArn": role,
            "PrincipalArn": principal,
            "SAMLAssertion": saml_assertion,
        }

        # Try the maximum duration of 12 hours, if it fails try to use the
        # maximum duration indicated by the error
        if self.config.auto_duration and auto_duration:
            sts_call_vars["DurationSeconds"] = self.config.max_duration
            try:
                res = self.sts_client.assume_role_with_saml(**sts_call_vars)
            except ClientError as err:
                if err.response.get("Error", []).get(
                    "Code"
                ) == "ValidationError" and err.response.get("Error", []).get("Message"):
                    m = re.search(
                        "Member must have value less than or equal to ([0-9]{3,5})",
                        err.response["Error"]["Message"],
                    )
                    if m is not None and m.group(1):
                        new_duration = int(m.group(1))
                        return self.assume_role(
                            role,
                            principal,
                            saml_assertion,
                            duration=new_duration,
                            auto_duration=False,
                        )
                # Unknown error or no max time returned in error message
                raise
        elif duration:
            sts_call_vars["DurationSeconds"] = duration

        res = self.sts_client.assume_role_with_saml(**sts_call_vars)

        return res

    def resolve_aws_aliases(self, roles):
        def resolve_aws_alias(role, principal, aws_dict):
            try:
                session = boto3.session.Session(region_name=self.config.region)

                sts = session.client(
                    "sts",
                    config=botocore.client.Config(signature_version=botocore.UNSIGNED),
                )
                saml = sts.assume_role_with_saml(
                    RoleArn=role,
                    PrincipalArn=principal,
                    SAMLAssertion=self.base64_encoded_saml,
                )

                iam = session.client(
                    "iam",
                    aws_access_key_id=saml["Credentials"]["AccessKeyId"],
                    aws_secret_access_key=saml["Credentials"]["SecretAccessKey"],
                    aws_session_token=saml["Credentials"]["SessionToken"],
                )

                response = iam.list_account_aliases()
                account_alias = response["AccountAliases"][0]
                aws_dict[role.split(":")[4]] = account_alias
            except Exception as err:
                logging.debug("Failing to resolve alias %s", err)
                aws_dict[role.split(":")[4]] = role.split(":")[4]

        threads = []
        aws_id_alias = {}
        for number, (role, principal) in enumerate(roles.items()):
            t = Thread(target=resolve_aws_alias, args=(role, principal, aws_id_alias))
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        return aws_id_alias

    @staticmethod
    def is_valid_saml_assertion(saml_xml):
        if saml_xml is None:
            return False

        try:
            doc = etree.fromstring(saml_xml)
            conditions = list(
                doc.iter(tag="{urn:oasis:names:tc:SAML:2.0:assertion}Conditions")
            )
            not_before_str = conditions[0].get("NotBefore")
            not_on_or_after_str = conditions[0].get("NotOnOrAfter")

            now = datetime.utcnow()
            not_before = datetime.strptime(not_before_str, "%Y-%m-%dT%H:%M:%S.%fZ")
            not_on_or_after = datetime.strptime(
                not_on_or_after_str, "%Y-%m-%dT%H:%M:%S.%fZ"
            )

            if not_before <= now < not_on_or_after:
                return True
            else:
                return False
        except Exception:
            return False
