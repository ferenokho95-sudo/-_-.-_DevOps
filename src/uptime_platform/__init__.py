import uuid
from typing import Annotated

import dagger
from dagger import dag, function, object_type, DefaultPath, Ignore


@object_type
class UptimePlatform:
    async def run(self, action: str, source: dagger.Directory,
                  socket: dagger.Socket | None, credentials: dagger.Secret | None,
                  target: str = "kuma", mode: str = "crash") -> str:
        if credentials is None:
            raise ValueError("Pass --credentials=file:.env. Secrets must be supplied by the calling CLI, not read from module source.")
        if socket is None:
            raise ValueError("Pass --socket=/var/run/docker.sock to grant access to the host Docker daemon.")
        runner = (dag.container().from_("docker:28.1.1-cli")
                  .with_exec(["apk", "add", "--no-cache", "python3"])
                  .with_directory("/workspace", source)
                  .with_workdir("/workspace")
                  .with_unix_socket("/var/run/docker.sock", socket)
                  .with_mounted_secret("/run/credentials", credentials)
                  .with_env_variable("RUN_NONCE", str(uuid.uuid4())))
        return await runner.with_exec(["python3", "scripts/control.py", action, target, mode]).stdout()

    @function
    async def deploy_app(self, source: Annotated[dagger.Directory, DefaultPath("."), Ignore([".env", ".git", "__pycache__", ".venv", "sdk"])],
                         socket: dagger.Socket | None = None,
                         credentials: dagger.Secret | None = None) -> str:
        return await self.run("deploy-app", source, socket, credentials)

    @function
    async def deploy_monitoring(self, source: Annotated[dagger.Directory, DefaultPath("."), Ignore([".env", ".git", "__pycache__", ".venv", "sdk"])],
                                socket: dagger.Socket | None = None,
                                credentials: dagger.Secret | None = None) -> str:
        return await self.run("deploy-monitoring", source, socket, credentials)

    @function
    async def configure_grafana(self, source: Annotated[dagger.Directory, DefaultPath("."), Ignore([".env", ".git", "__pycache__", ".venv", "sdk"])],
                                socket: dagger.Socket | None = None,
                                credentials: dagger.Secret | None = None) -> str:
        return await self.run("configure-grafana", source, socket, credentials)

    @function
    async def deploy_all(self, source: Annotated[dagger.Directory, DefaultPath("."), Ignore([".env", ".git", "__pycache__", ".venv", "sdk"])],
                         socket: dagger.Socket | None = None,
                         credentials: dagger.Secret | None = None) -> str:
        return await self.run("deploy-all", source, socket, credentials)

    @function
    async def chaos(self, source: Annotated[dagger.Directory, DefaultPath("."), Ignore([".env", ".git", "__pycache__", ".venv", "sdk"])],
                    socket: dagger.Socket | None = None,
                    credentials: dagger.Secret | None = None,
                    target: str = "kuma", mode: str = "crash") -> str:
        return await self.run("chaos", source, socket, credentials, target, mode)

    @function
    async def validate(self, source: Annotated[dagger.Directory, DefaultPath("."), Ignore([".env", ".git", "__pycache__", ".venv", "sdk"])],
                       socket: dagger.Socket | None = None,
                       credentials: dagger.Secret | None = None) -> str:
        """Check the running platform without recreating containers."""
        return await self.run("validate", source, socket, credentials)

    @function
    async def persistence_check(self, source: Annotated[dagger.Directory, DefaultPath("."), Ignore([".env", ".git", "__pycache__", ".venv", "sdk"])],
                                socket: dagger.Socket | None = None,
                                credentials: dagger.Secret | None = None) -> str:
        """Replace app containers and check that SQL and Kuma files survive."""
        return await self.run("persistence-check", source, socket, credentials)

