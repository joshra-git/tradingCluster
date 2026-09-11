"""
Creates and deletes agent Deployments (not bare Pods) via the Kubernetes API,
so every agent self-heals on crash or accidental deletion the same way we set
up manually for agent-001. No CRD/operator framework here on purpose - the
Controller itself IS the reconciliation loop, and Postgres (not a CR's
.status) is the source of truth for agent state.
"""
import os
from kubernetes import client, config as kconfig

NAMESPACE = os.environ.get("NAMESPACE", "trading")
AGENT_IMAGE = os.environ["AGENT_IMAGE"]
CONTROLLER_URL = os.environ.get("CONTROLLER_INTERNAL_URL", "http://controller.trading.svc.cluster.local:8080")

try:
    kconfig.load_incluster_config()
except Exception:
    kconfig.load_kube_config()

apps_v1 = client.AppsV1Api()


def _container_spec(agent_name, strategy):
    return client.V1Container(
        name="agent",
        image=AGENT_IMAGE,
        image_pull_policy="IfNotPresent",  # image is loaded locally into kind, never pulled from a registry
        env=[
            client.V1EnvVar(name="AGENT_NAME", value=agent_name),
            client.V1EnvVar(name="CONTROLLER_URL", value=CONTROLLER_URL),
            client.V1EnvVar(name="STRATEGY_SYMBOLS", value=strategy.get("symbols", "SPY,QQQ")),
            client.V1EnvVar(
                name="ANTHROPIC_API_KEY",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(name="trading-secrets", key="ANTHROPIC_API_KEY")
                ),
            ),
        ],
        resources=client.V1ResourceRequirements(
            requests={"cpu": "50m", "memory": "128Mi"},
            limits={"cpu": "250m", "memory": "256Mi"},
        ),
    )


def spawn_agent_pod(agent_name, strategy):
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=agent_name,
            namespace=NAMESPACE,
            labels={"app": "trading-agent", "agent-name": agent_name},
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels={"agent-name": agent_name}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels={"app": "trading-agent", "agent-name": agent_name}),
                spec=client.V1PodSpec(
                    restart_policy="Always",
                    containers=[_container_spec(agent_name, strategy)],
                ),
            ),
        ),
    )
    apps_v1.create_namespaced_deployment(namespace=NAMESPACE, body=deployment)


def delete_agent_pod(agent_name):
    try:
        apps_v1.delete_namespaced_deployment(name=agent_name, namespace=NAMESPACE)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise


def pod_exists(agent_name):
    try:
        apps_v1.read_namespaced_deployment(name=agent_name, namespace=NAMESPACE)
        return True
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return False
        raise
