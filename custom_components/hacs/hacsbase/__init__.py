"""Initialize the HACS base."""
# pylint: disable=unused-argument, bad-continuation
import json
import uuid
from datetime import timedelta

from homeassistant.helpers.event import async_call_later, async_track_time_interval

from aiogithubapi import AIOGitHubException, AIOGitHubRatelimit
from integrationhelper import Logger

from .task_factory import HacsTaskFactory
from .exceptions import HacsException

from ..const import ELEMENT_TYPES
from ..setup import setup_extra_stores
from ..store import async_load_from_store, async_save_to_store
from ..helpers.get_defaults import get_default_repos_lists, get_default_repos_orgs

from custom_components.hacs.helpers.register_repository import register_repository
from custom_components.hacs.globals import removed_repositories, get_removed
from custom_components.hacs.repositories.removed import RemovedRepository


class HacsStatus:
    """HacsStatus."""

    startup = True
    new = False
    background_task = False
    reloading_data = False
    upgrading_all = False


class HacsFrontend:
    """HacsFrontend."""

    version_running = None
    version_available = None
    update_pending = False


class HacsCommon:
    """Common for HACS."""

    categories = []
    blacklist = []
    default = []
    installed = []
    skip = []


class System:
    """System info."""

    status = HacsStatus()
    config_path = None
    ha_version = None
    disabled = False
    lovelace_mode = "storage"


class Developer:
    """Developer settings/tools."""

    template_id = "Repository ID"
    template_content = ""
    template_raw = ""

    @property
    def devcontainer(self):
        """Is it a devcontainer?"""
        import os

        if "DEVCONTAINER" in os.environ:
            return True
        return False


class Hacs:
    """The base class of HACS, nested thoughout the project."""

    token = f"{str(uuid.uuid4())}-{str(uuid.uuid4())}"
    hacsweb = f"/hacsweb/{token}"
    hacsapi = f"/hacsapi/{token}"
    repositories = []
    frontend = HacsFrontend()
    repo = None
    data_repo = None
    developer = Developer()
    data = None
    configuration = None
    logger = Logger("hacs")
    github = None
    hass = None
    version = None
    session = None
    factory = HacsTaskFactory()
    system = System()
    recuring_tasks = []
    common = HacsCommon()

    @staticmethod
    def init(hass, github_token):
        """Return a initialized HACS object."""
        return Hacs()

    def get_by_id(self, repository_id):
        """Get repository by ID."""
        try:
            for repository in self.repositories:
                if repository.information.uid == repository_id:
                    return repository
        except Exception:  # pylint: disable=broad-except
            pass
        return None

    def get_by_name(self, repository_full_name):
        """Get repository by full_name."""
        try:
            for repository in self.repositories:
                if repository.data.full_name.lower() == repository_full_name.lower():
                    return repository
        except Exception:  # pylint: disable=broad-except
            pass
        return None

    def is_known(self, repository_full_name):
        """Return a bool if the repository is known."""
        return repository_full_name.lower() in [
            x.data.full_name.lower() for x in self.repositories
        ]

    @property
    def sorted_by_name(self):
        """Return a sorted(by name) list of repository objects."""
        return sorted(self.repositories, key=lambda x: x.display_name)

    @property
    def sorted_by_repository_name(self):
        """Return a sorted(by repository_name) list of repository objects."""
        return sorted(self.repositories, key=lambda x: x.data.full_name)

    async def register_repository(self, full_name, category, check=True):
        """Register a repository."""
        await register_repository(full_name, category, check=True)

    async def startup_tasks(self):
        """Tasks tha are started after startup."""
        self.system.status.background_task = True
        await self.hass.async_add_executor_job(setup_extra_stores)
        self.hass.bus.async_fire("hacs/status", {})
        self.logger.debug(self.github.ratelimits.remaining)
        self.logger.debug(self.github.ratelimits.reset_utc)

        await self.handle_critical_repositories_startup()
        await self.handle_critical_repositories()
        await self.load_known_repositories()
        await self.clear_out_blacklisted_repositories()

        self.recuring_tasks.append(
            async_track_time_interval(
                self.hass, self.recuring_tasks_installed, timedelta(minutes=30)
            )
        )
        self.recuring_tasks.append(
            async_track_time_interval(
                self.hass, self.recuring_tasks_all, timedelta(minutes=800)
            )
        )

        self.hass.bus.async_fire("hacs/reload", {"force": True})
        await self.recuring_tasks_installed()

        self.system.status.startup = False
        self.system.status.new = False
        self.system.status.background_task = False
        self.hass.bus.async_fire("hacs/status", {})
        await self.data.async_write()

    async def handle_critical_repositories_startup(self):
        """Handled critical repositories during startup."""
        alert = False
        critical = await async_load_from_store(self.hass, "critical")
        if not critical:
            return
        for repo in critical:
            if not repo["acknowledged"]:
                alert = True
        if alert:
            self.logger.critical("URGENT!: Check the HACS panel!")
            self.hass.components.persistent_notification.create(
                title="URGENT!", message="**Check the HACS panel!**"
            )

    async def handle_critical_repositories(self):
        """Handled critical repositories during runtime."""
        # Get critical repositories
        instored = []
        critical = []
        was_installed = False

        try:
            critical = await self.data_repo.get_contents("critical")
            critical = json.loads(critical.content)
        except AIOGitHubException:
            pass

        if not critical:
            self.logger.debug("No critical repositories")
            return

        stored_critical = await async_load_from_store(self.hass, "critical")

        for stored in stored_critical or []:
            instored.append(stored["repository"])

        stored_critical = []

        for repository in critical:
            self.common.blacklist.append(repository["repository"])
            removed_repo = get_removed(repository["repository"])
            removed_repo.removal_type = "critical"
            repo = self.get_by_name(repository["repository"])

            stored = {
                "repository": repository["repository"],
                "reason": repository["reason"],
                "link": repository["link"],
                "acknowledged": True,
            }
            if repository["repository"] not in instored:
                if repo is not None and repo.installed:
                    self.logger.critical(
                        f"Removing repository {repository['repository']}, it is marked as critical"
                    )
                    was_installed = True
                    stored["acknowledged"] = False
                    # Uninstall from HACS
                    repo.remove()
                    await repo.uninstall()
            stored_critical.append(stored)
            removed_repo.update_data(stored)

        # Save to FS
        await async_save_to_store(self.hass, "critical", stored_critical)

        # Resart HASS
        if was_installed:
            self.logger.critical("Resarting Home Assistant")
            self.hass.async_create_task(self.hass.async_stop(100))

    async def recuring_tasks_installed(self, notarealarg=None):
        """Recuring tasks for installed repositories."""
        self.logger.debug(
            "Starting recuring background task for installed repositories"
        )
        self.system.status.background_task = True
        self.hass.bus.async_fire("hacs/status", {})
        self.logger.debug(self.github.ratelimits.remaining)
        self.logger.debug(self.github.ratelimits.reset_utc)
        for repository in self.repositories:
            if (
                repository.status.installed
                and repository.data.category in self.common.categories
            ):
                self.factory.tasks.append(self.factory.safe_update(repository))

        await self.factory.execute()
        await self.handle_critical_repositories()
        self.system.status.background_task = False
        self.hass.bus.async_fire("hacs/status", {})
        await self.data.async_write()
        self.logger.debug("Recuring background task for installed repositories done")

    async def recuring_tasks_all(self, notarealarg=None):
        """Recuring tasks for all repositories."""
        self.logger.debug("Starting recuring background task for all repositories")
        await self.hass.async_add_executor_job(setup_extra_stores)
        self.system.status.background_task = True
        self.hass.bus.async_fire("hacs/status", {})
        self.logger.debug(self.github.ratelimits.remaining)
        self.logger.debug(self.github.ratelimits.reset_utc)
        for repository in self.repositories:
            if repository.data.category in self.common.categories:
                self.factory.tasks.append(self.factory.safe_common_update(repository))

        await self.factory.execute()
        await self.load_known_repositories()
        await self.clear_out_blacklisted_repositories()
        self.system.status.background_task = False
        await self.data.async_write()
        self.hass.bus.async_fire("hacs/status", {})
        self.hass.bus.async_fire("hacs/repository", {"action": "reload"})
        self.logger.debug("Recuring background task for all repositories done")

    async def clear_out_blacklisted_repositories(self):
        """Clear out blaclisted repositories."""
        need_to_save = False
        for repository in self.common.blacklist:
            if self.is_known(repository):
                repository = self.get_by_name(repository)
                if repository.status.installed:
                    self.logger.warning(
                        f"You have {repository.data.full_name} installed with HACS "
                        + "this repository has been blacklisted, please consider removing it."
                    )
                else:
                    need_to_save = True
                    repository.remove()

        if need_to_save:
            await self.data.async_write()

    async def get_repositories(self):
        """Return a list of repositories."""
        repositories = {}
        for category in self.common.categories:
            repositories[category] = await get_default_repos_lists(
                self.session, self.configuration.token, category
            )
            org = await get_default_repos_orgs(self.github, category)
            for repo in org:
                repositories[category].append(repo)

        for category in repositories:
            for repo in repositories[category]:
                if repo not in self.common.default:
                    self.common.default.append(repo)
        return repositories

    async def load_known_repositories(self):
        """Load known repositories."""
        self.logger.info("Loading known repositories")
        repositories = await self.get_repositories()

        for item in await get_default_repos_lists(
            self.session, self.configuration.token, "blacklist"
        ):
            if item not in self.common.blacklist:
                removed = get_removed(item)
                removed.removal_type = "blacklist"
                self.common.blacklist.append(item)

        for category in repositories:
            for repo in repositories[category]:
                if repo in self.common.blacklist:
                    continue
                if self.is_known(repo):
                    continue
                self.factory.tasks.append(self.factory.safe_register(repo, category))
        await self.factory.execute()
        self.logger.info("Loading known repositories finished")
