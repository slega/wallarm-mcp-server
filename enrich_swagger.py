#!/usr/bin/env python3
"""
Swagger Enrichment Application

Flow:
1. Retrieve KB chunks with bedrock-agent-runtime.retrieve (this works in your env).
2. Rewrite KB text into clean English OAS style with bedrock-runtime.converse(modelId=...).
3. Sanitize in Python as last defense.
4. Log progress.
"""

import json
import os
import logging
import boto3
import time
import re
import string
from typing import Dict, Any, Optional
from botocore.exceptions import ClientError

# ------------------------------------------------------------
# logging
# ------------------------------------------------------------
log_group = os.getenv("CLOUDWATCH_LOG_GROUP", "/ec2/swagger-enricher")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)
logger.addHandler(console_handler)

try:
    import watchtower  # type: ignore

    region_for_logs = os.getenv("AWS_REGION", "us-east-2")
    cw_handler = watchtower.CloudWatchLogHandler(
        log_group=log_group,
        stream_name=os.getenv("HOSTNAME", "ec2-instance"),
        use_queues=False,
        create_log_group=True,
        boto3_client=boto3.client("logs", region_name=region_for_logs),
    )
    logger.addHandler(cw_handler)
    logger.info(f"CloudWatch logging enabled: {log_group}")
except Exception as e:
    logger.warning(f"Failed to initialize CloudWatch logging: {e}")

# ------------------------------------------------------------
# prompts
# ------------------------------------------------------------

KB_QUERY_PROMPT = """
# Role
You are a precise API documentation writer tasked with writing description for Wallarm API Documentation.
Wallarm API implements all operations that are done by users via the Wallarm Console.

# Guidelines
Based on the documentation about Wallarm product, you need to describe the field in a way that is easy to understand and use.
It is possible that you won't be able to find the information in the documentation, so you need to infer it from your knowledge about Wallarm product, name of the parameter, the provided context, the endpoint, location of the parameter.
Prefer clear, concrete language. Do not invent numbers, enums, or defaults that are not present. Limit to one to three sentences.
You MUST ALWAYS provide a description for the field.
Even if you don't know the information, you need to provide a description that is likely to be correct.
Provide only the description, nothing else.
YOU MUST ONLY PROVIDE RESPONSE IN ENGLISH.

# Additional information
Some of endpoints in Wallarm API may include negation filters.
If you see a parameter like !something then it is a negation filter for a parameter called something.

# Response wording guidelines
1. Do not repeat the name of the parameter, endpoint, and its place in your response.
2. Your answer should follow this structure: "<param name> is <what it is>. It can be used for <how it can be used>. <OPTIONAL - fill ONLY if you are 100% sure - any limitation that apply to this parameter.>"

# Task
Based on the above guidelines, what should be the description of the "{field_name}" field in {location_desc} of the {method} {endpoint} endpoint in the Wallarm API ?
"""


REWRITE_PROMPT = """You are an experienced technical writer crafting concise API parameter descriptions.
You will get:
1. The parameter name.
2. Context text from the Wallarm knowledge base (may include markdown, links, images, or other languages).

Follow the rules carefully:
- Write entirely in English.
- Use two sentences totalling 15-40 words.
- The first sentence must define what the parameter represents.
- The second sentence must explain how the parameter is used or why it matters.
- Do not copy raw lists, enumerations, or metadata strings from the context. Summarise them in clear prose instead.
- Avoid quoting sample values unless absolutely necessary to explain the concept.
- Do not mention the endpoint or location explicitly in the description.
- Do not use markdown, links, or bullet points.
- Format must be exactly: "<param name> is ... It can be used for ..."
- If the context is missing or unclear, make a best-effort inference based on your knowledge of the Wallarm product.

Parameter name: {param_name}
Endpoint: {endpoint}
Method: {method}
Location: {location_desc}

Relevant knowledge base excerpts:
{kb_text}
"""

POLISH_PROMPT = """You are refining a draft API parameter description to make it clear, polished, and user-friendly.

Guidelines:
- Keep the output in English.
- Produce two sentences, 15-35 words total.
- Sentence 1: explain what the parameter is or represents.
- Sentence 2: describe how the parameter is used, why it is important, or what effect it has.
- Do not include redundant phrases, long enumerations, or metadata strings copied from the context.
- Avoid mentioning specific sample IDs or customer data unless essential.
- Do not refer to the endpoint, request location, or the fact that this is a description.
- Format must be exactly: "<param name> is ... It can be used for ..."
- If the draft already follows the rules but sounds mechanical, rephrase it to sound natural while keeping accurate meaning.

Parameter name: {param_name}
Endpoint: {endpoint}
Method: {method}
Location: {location_desc}
Draft description:
{draft_description}

Additional context (optional):
{kb_text}
"""

# ------------------------------------------------------------
# enricher
# ------------------------------------------------------------

class SwaggerEnricher:
    def __init__(
        self,
        knowledge_base_id: str,
        region: str = "us-east-2"
    ):
        self.knowledge_base_id = knowledge_base_id
        self.region = region
        self.model_arn = "arn:aws:bedrock:us-east-2:381492110259:inference-profile/us.amazon.nova-pro-v1:0"

        self.bedrock_agent = boto3.client("bedrock-agent-runtime", region_name=region)
        self.bedrock_runtime = boto3.client("bedrock-runtime", region_name=region)

        self.stats = {
            "properties_processed": 0,
            "descriptions_added": 0,
            "kb_queries": 0,
        }

        self.total_items_estimated = 0
        self.progress_log_every = 200
        self.last_query_time = 0.0
        self.min_query_interval = 0.1

    def _location_desc(self, location: str) -> str:
        mapping = {
            "body": "a parameter in the request body",
            "header": "a parameter in a request header",
            "path": "a parameter in the path",
            "query": "a query parameter",
            "response": "a parameter in the response body",
        }
        return mapping.get(location.lower(), "the request")

    def _log_progress(self) -> None:
        if not self.total_items_estimated:
            return
        done = self.stats["properties_processed"]
        pct = (done / self.total_items_estimated) * 100.0
        logger.info(f"Progress: {done} of about {self.total_items_estimated} ({pct:.1f}%)")

    # 1. retrieve from KB
    def _retrieve_kb_raw(
        self,
        field_name: str,
        endpoint: str,
        method: str,
        location: str,
    ) -> str:
        loc_desc = self._location_desc(location)
        query_text = KB_QUERY_PROMPT.format(
            field_name=field_name,
            location_desc=loc_desc,
            method=method,
            endpoint=endpoint,
        )

        # throttle
        now = time.time()
        dt = now - self.last_query_time
        if dt < self.min_query_interval:
            time.sleep(self.min_query_interval - dt)
        self.last_query_time = time.time()

        try:
            resp = self.bedrock_agent.retrieve(
                knowledgeBaseId=self.knowledge_base_id,
                retrievalQuery={"text": query_text},
                retrievalConfiguration={
                    "vectorSearchConfiguration": {"numberOfResults": 5}
                },
            )
            self.stats["kb_queries"] += 1
            pieces = []
            for item in resp.get("retrievalResults", []):
                ctext = item.get("content", {}).get("text")
                if ctext:
                    pieces.append(ctext)
            return "\n".join(pieces).strip()
        except Exception as e:
            logger.warning(f"KB retrieve failed for {field_name}: {e}")
            return ""

    # 2. rewrite with runtime.converse(modelId=...)
    def _rewrite_with_runtime(
        self,
        param_name: str,
        endpoint: str,
        method: str,
        location_desc: str,
        kb_text: str,
    ) -> Optional[str]:
        prompt = REWRITE_PROMPT.format(
            param_name=param_name,
            endpoint=endpoint,
            method=method,
            location_desc=location_desc,
            kb_text=(kb_text or "")[:4000],
        )
        try:
            resp = self.bedrock_runtime.converse(
                modelId=self.model_arn,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={
                    "maxTokens": 192,
                    "temperature": 0.2,
                    "topP": 0.9,
                },
            )
            content = resp.get("output", {}).get("message", {}).get("content", [])
            parts = []
            for c in content:
                if "text" in c:
                    parts.append(c["text"])
            text = " ".join(parts).strip()
            return text or None
        except Exception as e:
            logger.warning(f"rewrite runtime failed for {param_name}: {e}")
            return None

    def _polish_description(
        self,
        param_name: str,
        endpoint: str,
        method: str,
        location_desc: str,
        kb_text: str,
        draft_description: str,
    ) -> Optional[str]:
        prompt = POLISH_PROMPT.format(
            param_name=param_name,
            endpoint=endpoint,
            method=method,
            location_desc=location_desc,
            draft_description=draft_description,
            kb_text=(kb_text or "")[:4000],
        )
        try:
            resp = self.bedrock_runtime.converse(
                modelId=self.model_arn,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={
                    "maxTokens": 160,
                    "temperature": 0.15,
                    "topP": 0.85,
                },
            )
            content = resp.get("output", {}).get("message", {}).get("content", [])
            parts = []
            for c in content:
                if "text" in c:
                    parts.append(c["text"])
            text = " ".join(parts).strip()
            return text or None
        except Exception as e:
            logger.warning(f"polish runtime failed for {param_name}: {e}")
            return None

    def query_description(
        self,
        field_name: str,
        endpoint: str = "",
        method: str = "",
        location: str = "",
    ) -> str:
        kb_raw = self._retrieve_kb_raw(field_name, endpoint, method, location)
        loc_desc = self._location_desc(location)

        rewritten = self._rewrite_with_runtime(
            field_name, endpoint, method, loc_desc, kb_raw
        )
        if not rewritten:
            return ""

        polished = self._polish_description(
            field_name,
            endpoint,
            method,
            loc_desc,
            kb_raw,
            rewritten,
        )
        return polished or rewritten

    def enrich_property(
        self,
        prop: Dict[str, Any],
        prop_name: str,
        endpoint: str = "",
        method: str = "",
        location: str = "",
        parent_path: str = "",
    ) -> None:
        self.stats["properties_processed"] += 1

        if (
            self.total_items_estimated
            and self.stats["properties_processed"] % self.progress_log_every == 0
        ):
            self._log_progress()

        if prop.get("description"):
            return

        desc = self.query_description(
            prop_name, endpoint=endpoint, method=method, location=location
        )
        prop["description"] = desc
        self.stats["descriptions_added"] += 1
        logger.debug(f"Description for {parent_path}.{prop_name}: {desc}")

    def enrich_parameters(
        self,
        operation: Dict[str, Any],
        endpoint: str,
        method: str,
        path_item: Dict[str, Any],
    ) -> None:
        if "parameters" in path_item:
            for param in path_item["parameters"]:
                self._enrich_param_object(param, endpoint, method, f"{endpoint}.parameters")

        if "parameters" in operation:
            for param in operation["parameters"]:
                self._enrich_param_object(
                    param, endpoint, method, f"{endpoint}.{method}.parameters"
                )

        if "requestBody" in operation:
            content = operation["requestBody"].get("content", {})
            for _, content_schema in content.items():
                schema = content_schema.get("schema")
                if not schema:
                    continue
                self._enrich_schema_properties(
                    schema, endpoint, method, "body", f"{endpoint}.{method}.requestBody"
                )

    def _enrich_param_object(
        self,
        param: Dict[str, Any],
        endpoint: str,
        method: str,
        parent_path: str,
    ) -> None:
        param_location = param.get("in", "query").lower()

        if "schema" in param:
            schema = param["schema"]
            self._enrich_schema_properties(
                schema,
                endpoint,
                method,
                param_location,
                f"{parent_path}.{param.get('name','')}",
            )
            if not param.get("description"):
                desc = self.query_description(
                    param.get("name", ""), endpoint, method, param_location
                )
                param["description"] = desc
        else:
            if not param.get("description"):
                desc = self.query_description(
                    param.get("name", ""), endpoint, method, param_location
                )
                param["description"] = desc

    def _enrich_schema_properties(
        self,
        schema: Dict[str, Any],
        endpoint: str,
        method: str,
        location: str,
        parent_path: str,
    ) -> None:
        if not isinstance(schema, dict):
            return

        if "$ref" in schema:
            return

        if schema.get("type") == "array" and "items" in schema:
            self._enrich_schema_properties(
                schema["items"], endpoint, method, location, f"{parent_path}.items"
            )

        if "properties" in schema:
            for prop_name, prop in schema["properties"].items():
                self.enrich_property(
                    prop,
                    prop_name,
                    endpoint,
                    method,
                    location,
                    f"{parent_path}.{prop_name}",
                )
                self._enrich_schema_properties(
                    prop,
                    endpoint,
                    method,
                    location,
                    f"{parent_path}.{prop_name}",
                )

        for comp_key in ["allOf", "anyOf", "oneOf"]:
            if comp_key in schema:
                for idx, sub_schema in enumerate(schema[comp_key]):
                    self._enrich_schema_properties(
                        sub_schema,
                        endpoint,
                        method,
                        location,
                        f"{parent_path}.{comp_key}[{idx}]",
                    )

    def enrich_response(
        self,
        operation: Dict[str, Any],
        endpoint: str,
        method: str,
    ) -> None:
        if "responses" not in operation:
            return

        for status_code, response in operation["responses"].items():
            if "content" in response:
                for _, content_schema in response["content"].items():
                    schema = content_schema.get("schema")
                    if schema:
                        self._enrich_schema_properties(
                            schema,
                            endpoint,
                            method,
                            "response",
                            f"{endpoint}.{method}.responses.{status_code}",
                        )
            elif "schema" in response:
                schema = response["schema"]
                self._enrich_schema_properties(
                    schema,
                    endpoint,
                    method,
                    "response",
                    f"{endpoint}.{method}.responses.{status_code}",
                )

    def _estimate_total_items(self, swagger: Dict[str, Any]) -> int:
        count = 0
        paths = swagger.get("paths", {})
        for path_item in paths.values():
            for method, op in path_item.items():
                if not isinstance(op, dict):
                    continue
                if method not in [
                    "get",
                    "post",
                    "put",
                    "delete",
                    "patch",
                    "head",
                    "options",
                ]:
                    continue
                if "parameters" in path_item:
                    count += len(path_item["parameters"])
                if "parameters" in op:
                    count += len(op["parameters"])
                rb = op.get("requestBody", {})
                content = rb.get("content", {})
                for c in content.values():
                    schema = c.get("schema", {})
                    if "properties" in schema:
                        count += len(schema["properties"])
                resp = op.get("responses", {})
                count += len(resp)
        defs = swagger.get("definitions", {})
        count += len(defs)
        comps = swagger.get("components", {}).get("schemas", {})
        count += len(comps)
        return max(count, 1)

    def enrich_swagger(self, swagger: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Starting swagger enrichment...")

        self.total_items_estimated = self._estimate_total_items(swagger)
        logger.info(f"Estimated items to process: about {self.total_items_estimated}")

        if "paths" in swagger:
            logger.info("Processing paths...")
            for path, path_item in swagger["paths"].items():
                endpoint = path
                for method, operation in path_item.items():
                    if not isinstance(operation, dict):
                        continue
                    if method not in [
                        "get",
                        "post",
                        "put",
                        "delete",
                        "patch",
                        "head",
                        "options",
                    ]:
                        continue
                    upper_method = method.upper()
                    self.enrich_parameters(operation, endpoint, upper_method, path_item)
                    self.enrich_response(operation, endpoint, upper_method)

        if "definitions" in swagger:
            logger.info("Processing definitions...")
            for def_name, def_schema in swagger["definitions"].items():
                self._enrich_schema_properties(
                    def_schema, "", "", "", f"definitions.{def_name}"
                )

        if "components" in swagger and "schemas" in swagger["components"]:
            logger.info("Processing components/schemas...")
            for schema_name, schema in swagger["components"]["schemas"].items():
                self._enrich_schema_properties(
                    schema, "", "", "", f"components.schemas.{schema_name}"
                )

        self._log_progress()
        logger.info("Enrichment completed!")
        logger.info(f"Statistics: {self.stats}")
        return swagger

# ------------------------------------------------------------
# S3 helpers and main
# ------------------------------------------------------------

def save_to_s3(
    data: Dict[str, Any], bucket: str, key: str, region: str = "us-east-2"
) -> None:
    s3_client = boto3.client("s3", region_name=region)
    logger.info(f"Saving to S3: s3://{bucket}/{key}")
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data, indent=2, ensure_ascii=False),
        ContentType="application/json",
    )

def get_knowledge_base_id(
    knowledge_base_identifier: str, region: str = "us-east-2"
) -> str:
    if len(knowledge_base_identifier) == 10 and knowledge_base_identifier.isalnum():
        return knowledge_base_identifier

    logger.info(f"Looking up knowledge base ID for name: {knowledge_base_identifier}")
    bedrock_agent = boto3.client("bedrock-agent", region_name=region)
    paginator = bedrock_agent.get_paginator("list_knowledge_bases")
    for page in paginator.paginate():
        for kb in page.get("knowledgeBaseSummaries", []):
            if kb.get("name") == knowledge_base_identifier:
                kb_id = kb.get("knowledgeBaseId")
                logger.info(
                    f"Found knowledge base ID: {kb_id} for name: {knowledge_base_identifier}"
                )
                return kb_id

    raise ValueError(
        f"Knowledge base with name '{knowledge_base_identifier}' not found in region {region}"
    )

def verify_aws_credentials() -> None:
    sts_client = boto3.client("sts")
    identity = sts_client.get_caller_identity()
    logger.info(
        f"AWS credentials verified. Account: {identity.get('Account')}, ARN: {identity.get('Arn')}"
    )

def main():
    verify_aws_credentials()

    knowledge_base_identifier = "wallarm-docs"
    input_file = "wallarm-swager-raw.json"
    output_file = "wallarm-swagger-enriched.json"
    region = "us-east-2"
    s3_bucket = "swagger-enricher-prod-files-381492110259"

    knowledge_base_id = get_knowledge_base_id(knowledge_base_identifier, region)

    s3_client = boto3.client("s3", region_name=region)
    logger.info(f"Loading from S3: s3://{s3_bucket}/{input_file}")
    response = s3_client.get_object(Bucket=s3_bucket, Key=input_file)
    swagger = json.loads(response["Body"].read())

    enricher = SwaggerEnricher(
        knowledge_base_id,
        region
    )

    enriched_swagger = enricher.enrich_swagger(swagger)

    save_to_s3(enriched_swagger, s3_bucket, output_file, region)

    logger.info("Done!")
    logger.info(f"Final statistics: {enricher.stats}")

    for handler in logger.handlers:
        if hasattr(handler, "flush"):
            handler.flush()
        if hasattr(handler, "close"):
            handler.close()

if __name__ == "__main__":
    try:
        main()
    finally:
        for handler in logging.root.handlers:
            if hasattr(handler, "flush"):
                handler.flush()
            if hasattr(handler, "close"):
                handler.close()
