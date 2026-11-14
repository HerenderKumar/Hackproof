"""DockerRuntime against a real daemon. Runs inside the worker image (`make test-integration`)."""

import contextlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import docker
import docker.errors
import pytest

from worker.runtime import ATTEMPT_LABEL, DockerRuntime, Labelled

pytestmark = pytest.mark.integration

# Already present on any host that runs the compose stack; the containers are never started.
IMAGE = "postgres:16-alpine"


@dataclass(frozen=True)
class Leftovers:
    attempt: str
    container: str
    network: str
    unlabelled_container: str


@pytest.fixture
def leftovers() -> Iterator[Leftovers]:
    """What a crashed run leaves behind: a labelled network with a labelled container on it."""
    client = docker.from_env()
    attempt = str(uuid.uuid4())
    labels = {ATTEMPT_LABEL: attempt}
    network = client.networks.create(f"hp-{attempt}", internal=True, labels=labels)
    container = client.containers.create(IMAGE, labels=labels, network=network.name)
    unlabelled = client.containers.create(IMAGE)
    try:
        yield Leftovers(attempt, container.id, network.id, unlabelled.id)
    finally:
        for remove, item in (
            (client.api.remove_container, container.id),
            (client.api.remove_container, unlabelled.id),
            (client.api.remove_network, network.id),
        ):
            with contextlib.suppress(docker.errors.NotFound):
                remove(item)
        client.close()


async def test_ping_reaches_the_daemon() -> None:
    assert await DockerRuntime().ping() is True


async def test_only_labelled_containers_and_networks_are_listed(leftovers: Leftovers) -> None:
    runtime = DockerRuntime()

    containers = await runtime.attempt_containers()
    networks = await runtime.attempt_networks()

    assert Labelled(id=leftovers.container, attempt=leftovers.attempt) in containers
    assert Labelled(id=leftovers.network, attempt=leftovers.attempt) in networks
    assert leftovers.unlabelled_container not in {c.id for c in containers}


async def test_a_container_and_then_its_network_can_be_removed(leftovers: Leftovers) -> None:
    runtime = DockerRuntime()

    await runtime.remove_container(leftovers.container)
    await runtime.remove_network(leftovers.network)

    assert leftovers.container not in {c.id for c in await runtime.attempt_containers()}
    assert leftovers.network not in {n.id for n in await runtime.attempt_networks()}
