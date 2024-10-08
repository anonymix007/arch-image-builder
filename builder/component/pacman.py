import os
import pyalpm
import logging
import shutil
import libarchive
from logging import getLogger
from builder.lib.serializable import SerializableDict
from builder.lib.context import ArchBuilderContext
from builder.lib.config import ArchBuilderConfigError
from builder.lib.subscript import resolve_simple_values
log = getLogger(__name__)


def log_cb(level, line):
	if level & pyalpm.LOG_ERROR:
		ll = logging.ERROR
	elif level & pyalpm.LOG_WARNING:
		ll = logging.WARNING
	else: return
	log.log(ll, line.strip())


def dl_cb(filename, ev, data):
	match ev:
		case 0: log.debug(f"pacman downloading {filename}")
		case 2: log.warning(f"pacman retry download {filename}")
		case 3: log.info(f"pacman downloaded {filename}")


def progress_cb(target, percent, n, i):
	if len(target) <= 0 or percent != 0: return
	log.info(f"processing {target} ({i}/{n})")


class PacmanRepoServer(SerializableDict):
	url: str = None
	name: str = None
	mirror: bool = False

	def __init__(
		self,
		name: str = None,
		url: str = None,
		mirror: bool = None
	):
		if url is not None: self.url = url
		if name is not None: self.name = name
		if mirror is not None: self.mirror = mirror


class PacmanRepo(SerializableDict):
	name: str = None
	priority: int = 10000
	servers: list[PacmanRepoServer] = None
	mirrorlist: str = None
	publickey: str = None
	keyid: str = None

	def __init__(
		self,
		name: str = None,
		priority: int = None,
		servers: list[PacmanRepoServer] = None,
		mirrorlist: str = None,
		publickey: str = None,
		keyid: str = None
	):
		if name is not None: self.name = name
		if priority is not None: self.priority = priority
		if servers is not None: self.servers = servers
		else: self.servers = []
		if mirrorlist is not None: self.mirrorlist = mirrorlist
		if publickey is not None: self.publickey = publickey
		if keyid is not None: self.keyid = keyid

	def add_server(
		self,
		name: str = None,
		url: str = None,
		mirror: bool = None
	):
		self.servers.append(PacmanRepoServer(
			name=name,
			url=url,
			mirror=mirror,
		))


class Pacman:
	handle: pyalpm.Handle
	ctx: ArchBuilderContext
	root: str
	databases: dict[str: pyalpm.DB]
	config: dict
	caches: list[str]
	repos: list[PacmanRepo]

	def append_repos(self, lines: list[str], rootfs: bool = False):
		"""
		Add all databases into config
		"""
		for repo in self.repos:
			lines.append(f"[{repo.name}]\n")
			if rootfs and repo.mirrorlist is not None:
				lines.append(f"Include = /etc/pacman.d/{repo.name}-mirrorlist\n")
			else:
				for server in repo.servers:
					if server.mirror:
						lines.append(f"# Mirror {server.name}\n")
						log.debug(f"use mirror {server.name} url {server.url}")
					else:
						lines.append("# Original Repo\n")
						log.debug(f"use original repo url {server.url}")
					lines.append(f"Server = {server.url}\n")

	def append_config(self, lines: list[str]):
		"""
		Add basic pacman config for host
		"""
		siglevel = ("Required DatabaseOptional" if self.ctx.gpgcheck else "Never")
		lines.append("[options]\n")
		for cache in self.caches:
			lines.append(f"CacheDir = {cache}\n")
		lines.append(f"RootDir = {self.root}\n")
		lines.append(f"GPGDir = {self.handle.gpgdir}\n")
		lines.append(f"LogFile = {self.handle.logfile}\n")
		lines.append("HoldPkg = pacman glibc\n")
		lines.append(f"Architecture = {self.ctx.tgt_arch}\n")
		lines.append("UseSyslog\n")
		lines.append("Color\n")
		lines.append("CheckSpace\n")
		lines.append("VerbosePkgLists\n")
		lines.append("ParallelDownloads = 5\n")
		lines.append(f"SigLevel = {siglevel}\n")
		lines.append("LocalFileSigLevel = Optional\n")
		self.append_repos(lines)

	def init_keyring(self):
		"""
		Initialize pacman keyring
		"""
		path = os.path.join(self.ctx.work, "rootfs")
		keyring = os.path.join(path, "etc/pacman.d/gnupg")
		if not self.ctx.gpgcheck: return
		if os.path.exists(os.path.join(keyring, "trustdb.gpg")):
			log.debug("skip initialize pacman keyring when exists")
			return
		log.info("initializing pacman keyring")
		self.pacman_key(["--init"])

		# Download and add public keys and mirrorlist
		for repo in self.repos:
			if repo.mirrorlist is not None:
				mirrorlist = os.path.join(self.ctx.work, f"etc/pacman.d/{repo.name}-mirrorlist")
				cmds = ["wget", repo.mirrorlist, "-O", keypath]
				ret = self.ctx.run_external(cmds)
				if ret != 0: raise OSError(f"wget failed with {ret}")
			if repo.publickey is not None:
				keypath = os.path.join(self.ctx.work, f"{repo.name}.pub")
				cmds = ["wget", repo.publickey, "-O", keypath]
				ret = self.ctx.run_external(cmds)
				if ret != 0: raise OSError(f"wget failed with {ret}")
				self.pacman_key(["--add", keypath])
				self.lsign_key(repo.keyid)
			elif repo.keyid is not None:
				self.recv_keys(repo.keyid)
				self.lsign_key(repo.keyid)

	def init_config(self):
		"""
		Create host pacman.conf
		"""
		config = os.path.join(self.ctx.work, "pacman.conf")
		if os.path.exists(config):
			os.remove(config)
		log.info(f"generate pacman config {config}")
		lines = []
		self.append_config(lines)
		log.debug("config content: %s", "\t".join(lines).strip())
		log.debug(f"writing {config}")
		with open(config, "w") as f:
			f.writelines(lines)

	def pacman_key(self, args: list[str]):
		"""
		Call pacman-key for rootfs
		"""
		if not self.ctx.gpgcheck:
			raise RuntimeError("GPG check disabled")
		keyring = os.path.join(self.root, "etc/pacman.d/gnupg")
		config = os.path.join(self.ctx.work, "pacman.conf")
		cmds = ["pacman-key"]
		cmds.append(f"--gpgdir={keyring}")
		cmds.append(f"--config={config}")
		cmds.extend(args)
		ret = self.ctx.run_external(cmds)
		if ret != 0: raise OSError(f"pacman-key failed with {ret}")

	def pacman(self, args: list[str]):
		"""
		Call pacman for rootfs
		"""
		config = os.path.join(self.ctx.work, "pacman.conf")
		cmds = ["pacman"]
		cmds.append("--noconfirm")
		cmds.append(f"--root={self.root}")
		cmds.append(f"--config={config}")
		cmds.extend(args)
		ret = self.ctx.run_external(cmds)
		if ret != 0: raise OSError(f"pacman failed with {ret}")

	def load_databases(self):
		"""
		Add all databases and load them
		"""
		for mirror in self.repos:
			# register database
			if mirror.name not in self.databases:
				self.databases[mirror.name] = self.handle.register_syncdb(
					mirror.name, pyalpm.SIG_DATABASE_MARGINAL_OK
				)
			db = self.databases[mirror.name]

			# add databases servers
			servers: list[str] = []
			for server in mirror.servers:
				servers.append(server.url)
			db.servers = servers

			# update database now via pyalpm
			log.info(f"updating database {mirror.name}")
			db.update(False)
		self.init_config()
		self.refresh()

	def lookup_package(self, name: str) -> list[pyalpm.Package]:
		"""
		Lookup pyalpm package by name
		"""

		# pass a filename, load it directly
		if ".pkg.tar." in name:
			pkg = self.handle.load_pkg(name)
			if pkg is None: raise RuntimeError(f"load package {name} failed")
			return [pkg]

		s = name.split("/")
		if len(s) == 2:
			# use DATABASE/PACKAGE, find it in database
			if s[0] not in self.databases and s[0] != "local":
				raise ValueError(f"database {s[0]} not found")
			db = (self.handle.get_localdb() if s[0] == "local" else self.databases[s[0]])
			pkg = db.get_pkg(s[1])
			if pkg: return [pkg]
			raise ValueError(f"package {s[1]} not found")
		elif len(s) == 1:
			# use PACKAGE, find it in all databases or find as group

			# try find it as group
			pkg = pyalpm.find_grp_pkgs(self.databases.values(), name)
			if len(pkg) > 0: return pkg

			# try find it as package
			for dbn in self.databases:
				db = self.databases[dbn]
				pkg = db.get_pkg(name)
				if pkg: return [pkg]
			raise ValueError(f"package {name} not found")
		raise ValueError(f"bad package name {name}")

	def init_cache(self):
		"""
		Initialize pacman cache folder
		"""
		host_cache = "/var/cache/pacman/pkg" # host cache
		work_cache = os.path.join(self.ctx.work, "packages") # workspace cache
		root_cache = os.path.join(self.root, "var/cache/pacman/pkg") # rootfs cache
		self.caches.clear()

		# host cache is existing, use host cache folder
		if os.path.exists(host_cache):
			self.caches.append(host_cache)

		self.caches.append(work_cache)
		self.caches.append(root_cache)
		os.makedirs(work_cache, mode=0o0755, exist_ok=True)
		os.makedirs(root_cache, mode=0o0755, exist_ok=True)

	def add_repo(self, repo: PacmanRepo):
		if not repo or not repo.name or len(repo.servers) <= 0:
			raise ArchBuilderConfigError("bad repo")
		self.repos.append(repo)
		self.repos.sort(key=lambda r: r.priority)

	def init_repos(self):
		"""
		Initialize mirrors
		"""
		if "repo" not in self.config:
			raise ArchBuilderConfigError("no repos found in config")
		mirrors = self.ctx.get("mirrors", [])
		for repo in self.config["repo"]:
			if "name" not in repo:
				raise ArchBuilderConfigError("repo name not set")

			# never add local into database
			if repo["name"] == "local" or "/" in repo["name"]:
				raise ArchBuilderConfigError("bad repo name")

			# create pacman repo instance
			pacman_repo = PacmanRepo(name=repo["name"])
			if "priority" in repo:
				pacman_repo.priority = repo["priority"]

			if "mirrorlist" in repo:
				pacman_repo.mirrorlist = repo["mirrorlist"]

			# add public key url and id
			if "publickey" in repo and "keyid" not in repo:
				raise ArchBuilderConfigError("publickey is provided without keyid")

			if "publickey" in repo:
				pacman_repo.publickey = repo["publickey"]

			if "keyid" in repo:
				pacman_repo.keyid = repo["keyid"]

			originals: list[str] = []
			servers: list[str] = []

			# add all original repo url
			if "server" in repo: servers.append(repo["server"])
			if "servers" in repo: servers.extend(repo["server"])
			if len(servers) <= 0:
				raise ArchBuilderConfigError("no any original repo url found")

			# resolve original repo url
			for server in servers:
				originals.append(resolve_simple_values(server, {
					"arch": self.ctx.tgt_arch,
					"repo": repo["name"],
				}))

			# add repo mirror url
			for mirror in mirrors:
				if "name" not in mirror:
					raise ArchBuilderConfigError("mirror name not set")
				if "repos" not in mirror:
					raise ArchBuilderConfigError("repos list not set")
				for repo in mirror["repos"]:
					if "original" not in repo:
						raise ArchBuilderConfigError("original url not set")
					if "mirror" not in repo:
						raise ArchBuilderConfigError("mirror url not set")
					for original in originals:
						if original.startswith(repo["original"]):
							path = original[len(repo["original"]):]
							real_url = repo["mirror"] + path
							pacman_repo.add_server(
								name=mirror["name"],
								url=real_url,
								mirror=True,
							)

			# add original url
			for original in originals:
				pacman_repo.add_server(
					url=original,
					mirror=False
				)

			self.add_repo(pacman_repo)

	def __init__(self, ctx: ArchBuilderContext):
		"""
		Initialize pacman context
		"""
		self.ctx = ctx
		if "pacman" not in ctx.config:
			raise ArchBuilderConfigError("no pacman found in config")
		self.config = ctx.config["pacman"]
		self.root = ctx.get_rootfs()
		db = os.path.join(self.root, "var/lib/pacman")
		self.handle = pyalpm.Handle(self.root, db)
		self.handle.arch = ctx.tgt_arch
		self.handle.logfile = os.path.join(self.ctx.work, "pacman.log")
		self.handle.gpgdir = os.path.join(self.root, "etc/pacman.d/gnupg")
		self.handle.logcb = log_cb
		self.handle.dlcb = dl_cb
		self.handle.progresscb = progress_cb
		self.databases = {}
		self.caches = []
		self.repos = []
		self.init_cache()
		self.init_repos()
		for cache in self.caches:
			self.handle.add_cachedir(cache)
		self.init_config()

	def uninstall(self, pkgs: list[str]):
		"""
		Uninstall packages via pacman
		"""
		if len(pkgs) == 0: return
		ps = " ".join(pkgs)
		log.info(f"removing packages {ps}")
		args = ["--needed", "--remove"]
		args.extend(pkgs)
		self.pacman(args)

	def install(
		self,
		pkgs: list[str],
		/,
		force: bool = False,
		asdeps: bool = False,
		nodeps: bool = False,
	):
		"""
		Install packages via pacman
		"""
		if len(pkgs) == 0: return
		core_db = "var/lib/pacman/sync/core.db"
		if not os.path.exists(os.path.join(self.root, core_db)):
			self.refresh()
		ps = " ".join(pkgs)
		log.info(f"installing packages {ps}")
		args = ["--sync"]
		if not force: args.append("--needed")
		if asdeps: args.append("--asdeps")
		if nodeps: args.extend(["--nodeps", "--nodeps"])
		args.extend(pkgs)
		self.pacman(args)

	def download(self, pkgs: list[str]):
		"""
		Download packages via pacman
		"""
		if len(pkgs) == 0: return
		core_db = "var/lib/pacman/sync/core.db"
		if not os.path.exists(os.path.join(self.root, core_db)):
			self.refresh()
		log.info("downloading packages %s", " ".join(pkgs))
		args = ["--sync", "--downloadonly", "--nodeps", "--nodeps"]
		args.extend(pkgs)
		self.pacman(args)

	def install_local(self, files: list[str]):
		"""
		Install a local packages via pacman
		"""
		if len(files) == 0: return
		log.info("installing local packages %s", " ".join(files))
		args = ["--needed", "--upgrade"]
		args.extend(files)
		self.pacman(args)

	def refresh(self, /, force: bool = False):
		"""
		Update local databases via pacman
		"""
		log.info("refresh pacman database")
		args = ["--sync", "--refresh"]
		if force: args.append("--refresh")
		self.pacman(args)

	def recv_keys(self, keys: str | list[str]):
		"""
		Receive a key via pacman-key
		"""
		args = ["--recv-keys"]
		if type(keys) is str:
			args.append(keys)
		elif type(keys) is list:
			if len(keys) <= 0: return
			args.extend(keys)
		else: raise TypeError("bad keys type")
		self.pacman_key(args)

	def lsign_key(self, key: str):
		"""
		Local sign a key via pacman-key
		"""
		self.pacman_key(["--lsign-key", key])

	def pouplate_keys(
		self,
		names: str | list[str] = None,
		folder: str = None
	):
		"""
		Populate all keys via pacman-key
		"""
		args = ["--populate"]
		if folder: args.extend(["--populate-from", folder])
		if names is None: pass
		elif type(names) is str: args.append(names)
		elif type(names) is list: args.extend(names)
		else: raise TypeError("bad names type")
		self.pacman_key(args)

	def find_package_file(self, pkg: pyalpm.Package) -> str | None:
		"""
		Find out pacman package archive file in cache
		"""
		for cache in self.caches:
			p = os.path.join(cache, pkg.filename)
			if os.path.exists(p): return p
		return None

	def trust_keyring_pkg(self, pkg: pyalpm.Package):
		"""
		Trust a keyring package from file without install it
		"""
		if not self.ctx.gpgcheck: return
		names: list[str] = []
		target = os.path.join(self.ctx.work, "keyrings")
		keyring = "usr/share/pacman/keyrings/"

		# find out file path
		path = self.find_package_file(pkg)

		# cleanup keyring extract folder
		if os.path.exists(target):
			shutil.rmtree(target)
		os.makedirs(target, mode=0o0755)
		if path is None: raise RuntimeError(
			f"package {pkg.name} not found"
		)

		# open keyring package to extract
		log.debug(f"processing keyring package {pkg.name}")
		with libarchive.file_reader(path) as archive:
			for file in archive:
				pn: str = file.pathname
				if not pn.startswith(keyring): continue

				# get the filename of file
				fn = pn[len(keyring):]
				if len(fn) <= 0: continue

				# add keyring name to populate
				if fn.endswith(".gpg"): names.append(fn[:-4])

				# extract file
				dest = os.path.join(target, fn)
				log.debug(f"extracting {pn} to {dest}")
				with open(dest, "wb") as f:
					for block in file.get_blocks(file.size):
						f.write(block)
					fd = f.fileno()
					os.fchmod(fd, file.mode)
					os.fchown(fd, file.uid, file.gid)

		# trust extracted keyring
		self.pouplate_keys(names, target)

	def add_trust_keyring_pkg(self, pkgnames: list[str]):
		"""
		Trust a keyring package from file without install it
		"""
		if not self.ctx.gpgcheck: return
		if len(pkgnames) <= 0: return
		self.download(pkgnames)
		for pkgname in pkgnames:
			pkgs = self.lookup_package(pkgname)
			for pkg in pkgs:
				self.trust_keyring_pkg(pkg)
