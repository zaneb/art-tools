"""
For this command to work, https://github.com/openshift/check-payload binary has to exist in PATH and run as root
"""
import asyncio
import json
import os
import click
import koji
from doozerlib.cli import cli, pass_runtime, click_coroutine
from doozerlib.runtime import Runtime
from typing import Optional
from artcommonlib.exectools import cmd_gather_async, limit_concurrency, cmd_gather


class ScanFipsCli:
    def __init__(self, runtime: Runtime, nvrs: Optional[list]):
        self.runtime = runtime
        self.nvrs = nvrs

        # Initialize runtime and brewhub session
        self.runtime.initialize(clone_distgits=False)
        self.koji_session = koji.ClientSession(self.runtime.group_config.urls.brewhub)

    @limit_concurrency(os.cpu_count())
    async def run_get_problem_nvrs(self, build: tuple):
        # registry-proxy.engineering.redhat.com/rh-osbs/openshift-ose-sriov-network-operator@sha256:da95750d31cb1b9539f664d2d6255727fa8d648e93150ae92ed84a9e993753be
        nvr, pull_spec = build

        rc_scan, out_scan, _ = await cmd_gather_async(f"sudo check-payload scan image --spec {pull_spec}")

        # Eg: registry-proxy.engineering.redhat.com/rh-osbs/openshift-ose-sriov-network-operator
        # from registry-proxy.engineering.redhat.com/rh-osbs/openshift-ose-sriov-network-operator@sha256:da95750d31cb1b9539f664d2d6255727fa8d648e93150ae92ed84a9e993753be
        name_without_sha = pull_spec.split("@")[0]

        # c706f2c4 registry-proxy.engineering.redhat.com/rh-osbs/openshift-ose-aws-pod-identity-webhook
        rc, out, err = cmd_gather("sudo podman images --format '{{.ID}} {{.Repository}}'")
        if rc != 0:
            # This does not have a FIPS vulnerability, but we need to figure out why this failing to clean
            # since memory is limited in buildvm
            return build

        # `out` has multiple lines in {{.ID}} {{.Repository}} format
        for image in out.strip().split("\n"):
            if not image:
                # Skip null values
                continue

            image_id, image_name = image.split(" ")  # c706f2c4, registry-proxy.engineering.redhat.com/rh-osbs/openshift-ose-aws-pod-identity-webhook
            if name_without_sha == image_name:
                self.runtime.logger.info(f"Trying to clean image {image}")
                rmi_cmd_rc, _, rmi_cmd_err = cmd_gather(f"sudo podman rmi {image_id}")

                if rmi_cmd_rc != 0:
                    # This does not have a FIPS vulnerability, but we need to figure out why this failing to clean
                    # since memory is limited in buildvm
                    return build

        # The command will fail if it's not run on root, so need to make sure of that first during debugging
        # If it says successful run, it means that the command ran correctly
        return None if rc_scan == 0 and "Successful run" in out_scan else build

    async def run(self):
        # Get the list of NVRs to scan for
        # (nvr, pull-spec) list of tuples
        image_pullspec_mapping = []

        for nvr in self.nvrs:
            # Skip CI builds since it won't be shipped
            if nvr.startswith("ci-openshift"):
                self.runtime.logger.info(f"Skipping {nvr} since its a CI build")
                continue

            # Find the registry pull spec
            build_info = self.koji_session.getBuild(nvr)

            # Identify if its an RPM, and skip it
            if "git+https://pkgs.devel.redhat.com/git/rpms/" in build_info["source"]:
                self.runtime.logger.info(f"Skipping {nvr} since its an RPM")
                continue

            # Eg registry-proxy.engineering.redhat.com/rh-osbs/openshift-ose-sriov-network-operator@sha256:da95750d31cb1b9539f664d2d6255727fa8d648e93150ae92ed84a9e993753be
            # from https://brewweb.engineering.redhat.com/brew/buildinfo?buildID=2777601
            pull_spec = build_info["extra"]["image"]["index"]["pull"][0]
            image_pullspec_mapping.append((nvr, pull_spec))

        tasks = []
        for build in image_pullspec_mapping:
            tasks.append(self.run_get_problem_nvrs(build))

        results = await asyncio.gather(*tasks)

        problem_images = {}
        for build in results:
            if build:
                problem_images[build[0]] = build[1]

        self.runtime.logger.info(f"Found FIPS issues for these components: {problem_images}")
        click.echo(json.dumps(problem_images))


@cli.command("images:scan-fips", help="Trigger FIPS check for specified NVRs")
@click.option("--nvrs", required=False, help="Comma separated list to trigger scans for")
@pass_runtime
@click_coroutine
async def scan_fips(runtime: Runtime, nvrs: str):
    fips_pipeline = ScanFipsCli(runtime=runtime,
                                nvrs=nvrs.split(",") if nvrs else None
                                )
    await fips_pipeline.run()
