import argparse
import dataclasses
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import typing

import requests
import structlog
import tenacity
from dataclasses_json import dataclass_json

logger = structlog.get_logger()


@dataclass_json
@dataclasses.dataclass
class GitSpec:
    url: str
    ref: typing.Optional[str] = None
    depth: typing.Optional[int] = None


@dataclass_json
@dataclasses.dataclass
class HashSpec:
    algorithm: typing.Literal["sha1", "sha256"]
    value: str


@dataclass_json
@dataclasses.dataclass
class ArchiveFileSpec:
    url: str
    extension: typing.Optional[str] = None
    digest: typing.Optional[HashSpec] = None


@dataclass_json
@dataclasses.dataclass
class Project:
    name: str
    git: typing.Optional[GitSpec] = None
    archive: typing.Optional[ArchiveFileSpec] = None


@dataclass_json
@dataclasses.dataclass
class ProjectDefsJson:
    projects: typing.List[Project]

    @staticmethod
    def parse(json_str: str) -> typing.Dict[str, Project]:
        project_defs = ProjectDefsJson.from_json(json_str)
        return {project.name: project for project in project_defs.projects}


class OpenGrokAPIClient:
    """OpenGrok APIへのHTTPリクエストを担当するクラス"""

    def __init__(self, url: str):
        self.url = url

    def get_project_names(self) -> list[str]:
        """OpenGrok APIからプロジェクト名のリストを取得"""
        response = requests.get(f"{self.url}/projects")
        response.raise_for_status()
        return response.json()

    def add_project(self, project_name: str):
        """OpenGrok APIにプロジェクトを追加"""
        response = requests.post(
            f"{self.url}/projects",
            headers={"Content-Type": "text/plain"},
            data=project_name
        )
        response.raise_for_status()

    def delete_project(self, project_name: str):
        """OpenGrok APIからプロジェクトを削除"""
        response = requests.delete(f"{self.url}/projects/{project_name}")
        response.raise_for_status()

    def get_configuration(self) -> bytes:
        """OpenGrok APIから設定ファイルを取得"""
        response = requests.get(f"{self.url}/configuration")
        response.raise_for_status()
        return response.content


class ProjectJsonManager:
    """project.jsonファイルの読み書き操作を担当するクラス"""

    def __init__(self, data_dir: pathlib.Path, src_dir: pathlib.Path):
        self.data_dir = data_dir
        self.src_dir = src_dir  # マイグレーション専用（将来的に廃止予定）

    def _get_project_json_path(self, project_name: str) -> pathlib.Path:
        """project.jsonファイルのパスを生成"""
        return self.data_dir / project_name / "project.json"

    def migrate_project(self, project_name: str):
        """project.jsonファイルのマイグレーションを実行。"""
        new_path = self._get_project_json_path(project_name)

        # マイグレーション対象の古いパスを定義
        migration_sources = [
            self.src_dir / f"{project_name}.project.json",
            self.data_dir / f"{project_name}.project.json",
        ]

        for old_path in migration_sources:
            if not old_path.exists():
                continue

            try:
                if not new_path.exists():
                    # 移動先のディレクトリを作成
                    new_path.parent.mkdir(parents=True, exist_ok=True)
                    # ファイルを移動
                    shutil.move(str(old_path), str(new_path))
                    logger.info("Migrated project.json", project_name=project_name,
                                from_path=str(old_path),
                                to_path=str(new_path))
                else:
                    # new_pathが既に存在する場合は古いファイルを削除
                    old_path.unlink()
                    logger.info("Removed old project.json", project_name=project_name,
                                path=str(old_path))
            except Exception as e:
                raise Exception(f"Failed to migrate project.json from {old_path} to {new_path}") from e

    def load_project(self, project_name: str) -> typing.Optional[Project]:
        """project.jsonファイルからプロジェクト情報を読み込む"""
        self.migrate_project(project_name)
        project_json_path = self._get_project_json_path(project_name)
        if not project_json_path.exists():
            return None

        try:
            with open(project_json_path, "r") as f:
                project = Project.from_json(f.read())
                if project.name != project_name:
                    raise ValueError(f"Project name mismatch: {project.name} != {project_name}")
                return project
        except Exception:
            return None

    def save_project(self, project: Project):
        """project.jsonファイルにプロジェクト情報を保存"""
        self.migrate_project(project.name)
        project_json_path = self._get_project_json_path(project.name)
        project_json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(project_json_path, "w") as f:
            f.write(project.to_json(indent=2))

    def delete_project(self, project_name: str):
        """project.jsonファイルを削除"""
        self.migrate_project(project_name)
        project_json_path = self._get_project_json_path(project_name)
        project_json_path.unlink(missing_ok=True)


class SourceCodeDownloader:
    """ソースコードのダウンロードを担当するクラス"""

    def __init__(self, src_dir: pathlib.Path, data_dir: pathlib.Path, json_manager: ProjectJsonManager):
        self.src_dir = src_dir
        self.data_dir = data_dir
        self.json_manager = json_manager

    def download(self, project: Project) -> bool:
        """プロジェクトのソースコードをダウンロード
        
        Returns:
            bool: 変更があった場合はTrue、変更がなかった場合はFalse
        """
        target_dir = self.src_dir / project.name

        # git, archiveの順で確認し、最初に見つかったものを使用
        if project.git is not None:
            return self._download_git(project, target_dir)
        elif project.archive is not None:
            return self._download_archive(project, target_dir)
        else:
            raise ValueError(f"Project {project.name} has no git or archive specification")

    def _download_git(self, project: Project, target_dir: pathlib.Path) -> bool:
        """Git形式でソースコードをダウンロード
        
        Returns:
            bool: 変更があった場合はTrue、変更がなかった場合はFalse
        """
        git_spec = project.git
        if git_spec is None:
            raise ValueError(f"Project {project.name} has no git specification")

        # 既存リポジトリを使用するかどうかを判定
        old_project = self.json_manager.load_project(project.name)
        use_existing_repo = (
                target_dir.exists()
                and (target_dir / ".git").exists()
                and old_project == project
        )

        if use_existing_repo:
            # 既存ディレクトリがある場合: git fetch && git reset --hard origin/<ref>
            # fetch前のHEAD commit idを取得
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=target_dir,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
                text=True,
            )
            old_commit_id = result.stdout.strip()

            subprocess.run(
                ["git", "fetch", "origin"],
                cwd=target_dir,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            # fetch後、refがタグかブランチかを判定して適切にresetする
            ref = git_spec.ref or "HEAD"

            # まずタグとして試す
            result = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/tags/{ref}"],
                cwd=target_dir,
                capture_output=True,
            )
            if result.returncode == 0:
                # タグの場合
                subprocess.run(
                    ["git", "reset", "--hard", ref],
                    cwd=target_dir,
                    check=True,
                    stdin=subprocess.DEVNULL,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                )
            else:
                # ブランチの場合
                subprocess.run(
                    ["git", "reset", "--hard", f"origin/{ref}"],
                    cwd=target_dir,
                    check=True,
                    stdin=subprocess.DEVNULL,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                )

            # fetch後のHEAD commit idを取得
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=target_dir,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
                text=True,
            )
            new_commit_id = result.stdout.strip()

            # commit idが変化していた場合はTrue、同一の場合はFalse
            return old_commit_id != new_commit_id
        else:
            # 既存ディレクトリがない場合: git clone
            if target_dir.exists():
                shutil.rmtree(target_dir)

            clone_cmd = ["git", "clone"]
            if git_spec.depth is not None:
                clone_cmd.extend(["--depth", str(git_spec.depth)])
            if git_spec.ref:
                clone_cmd.extend(["--branch", git_spec.ref])
            clone_cmd.extend([git_spec.url, str(target_dir)])

            subprocess.run(
                clone_cmd,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )

            # 新規クローンの場合は常にTrue
            return True

    def _download_archive(self, project: Project, target_dir: pathlib.Path) -> bool:
        """アーカイブ形式でソースコードをダウンロード
        
        Returns:
            bool: 変更があった場合はTrue、変更がなかった場合はFalse
        """
        archive_spec = project.archive
        if archive_spec is None:
            raise ValueError(f"Project {project.name} has no archive specification")

        # 拡張子を判定
        extension = archive_spec.extension
        if extension is None:
            # URLから拡張子を抽出
            url_path = pathlib.Path(archive_spec.url)
            # .tar.gz, .tar.bz2, .tar.xz, .tar.zstなどの複合拡張子に対応
            if url_path.suffixes:
                if len(url_path.suffixes) >= 2 and url_path.suffixes[-2] == ".tar":
                    extension = "".join(url_path.suffixes[-2:])
                else:
                    extension = url_path.suffixes[-1]
            else:
                raise ValueError(f"Cannot determine file extension from URL: {archive_spec.url}")

        # 拡張子を正規化（先頭のドットを削除）
        extension = extension.lstrip(".")
        extension_lower = extension.lower()

        # 既存ディレクトリがある場合、以前のProject情報と比較
        if target_dir.exists():
            old_project = self.json_manager.load_project(project.name)
            if old_project is not None and old_project.archive == archive_spec:
                # 一致する場合: 何もしない
                return False
            # 異なる場合: ディレクトリを削除
            shutil.rmtree(target_dir)

        # HTTPダウンロード
        response = requests.get(archive_spec.url, stream=True)
        response.raise_for_status()

        # data_dirに保存（${data_dir}/${project_name}/archive.${extension}）
        archive_dir = self.data_dir / project.name
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"archive.{extension}"

        try:
            # アーカイブファイルをダウンロード
            with open(archive_path, "wb") as archive_file:
                for chunk in response.iter_content(chunk_size=8192):
                    archive_file.write(chunk)
                archive_file.flush()

            # ハッシュ検証
            if archive_spec.digest is not None:
                self._verify_hash(archive_path, archive_spec.digest)

            # アーカイブ展開
            target_dir.mkdir(parents=True, exist_ok=True)

            if extension_lower == "zip":
                # ZIP形式で展開（unzipコマンドを使用）
                result = subprocess.run(
                    ["unzip", "-q", str(archive_path), "-d", str(target_dir)],
                    check=True,
                    stdin=subprocess.DEVNULL,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                )
            elif extension_lower == "tar" or extension_lower.startswith("tar."):
                # TAR形式で展開（.tar, .tar.gz, .tar.bz2, .tar.xz, .tar.zstなど）
                # tar axfで自動的に圧縮形式を検出して展開
                try:
                    subprocess.run(
                        ["tar", "axf", str(archive_path), "-C", str(target_dir)],
                        check=True,
                        stdin=subprocess.DEVNULL,
                        stdout=sys.stdout,
                        stderr=sys.stderr,
                    )
                except subprocess.CalledProcessError as e:
                    raise ValueError(
                        f"Failed to extract archive file: {extension}. Error: {e}"
                    ) from e
            else:
                raise ValueError(f"Unsupported archive format: {extension}")
        except Exception:
            # エラーが発生した場合は、不正になっていると思われるデータを削除
            archive_path.unlink(missing_ok=True)
            if target_dir.exists():
                shutil.rmtree(target_dir)
            logger.error("Failed to download and extract archive file", project_name=project.name)
            raise

        # ダウンロード/再ダウンロードが実行された場合はTrue
        return True

    def _verify_hash(self, file_path: pathlib.Path, digest: HashSpec):
        """ファイルのハッシュ値を検証"""
        hash_obj = hashlib.sha1() if digest.algorithm == "sha1" else hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hash_obj.update(chunk)
        computed_hash = hash_obj.hexdigest()
        if computed_hash != digest.value:
            raise ValueError(
                f"Hash mismatch: expected {digest.value}, got {computed_hash}"
            )


class OpenGrokClient:
    """OpenGrokAPIClientとProjectJsonManagerを統合し、統一的なインターフェースを提供するクラス"""

    def __init__(self, base_uri: str, src_dir: pathlib.Path, data_dir: pathlib.Path):
        self.base_uri = base_uri
        self.src_dir = src_dir
        self.data_dir = data_dir
        self.api_client = OpenGrokAPIClient(f"{base_uri}/api/v1")
        self.json_manager = ProjectJsonManager(data_dir, src_dir)

    def get_projects(self) -> dict[str, Project]:
        # OpenGrok APIからプロジェクト名のリストを取得
        project_names = self.api_client.get_project_names()

        projects = {}
        invalid_project_names = set()
        for project_name in project_names:
            # 各プロジェクトの詳細メタデータをファイルから読み込む
            project = self.json_manager.load_project(project_name)
            if project is not None:
                projects[project_name] = project
            else:
                # ファイルが存在しない、もしくはproject.jsonが不正な場合はエラー処理とする。
                invalid_project_names.add(project_name)

        for project_name in invalid_project_names:
            self.delete_project(Project(name=project_name))

        return projects

    def add_project(self, project: Project):
        # https://github.com/oracle/opengrok/wiki/Per-project-management-and-workflow
        # opengrok-projadmコマンドでプロジェクトを追加
        cmd = [
            "opengrok-projadm",
            "--jar", "/opengrok/lib/opengrok.jar",
            "--base", "/opengrok/",
            "--uri", self.base_uri,
            "--add", project.name,
        ]
        subprocess.run(
            cmd,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )

        # 再生成したconfigをreload
        # 設定ファイルをAPIから取得して一時ファイルに保存
        with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.xml') as tmp_file:
            tmp_config_path = pathlib.Path(tmp_file.name)
            try:
                config_content = self.api_client.get_configuration()
                tmp_file.write(config_content)
                tmp_file.flush()

                # opengrok-indexerを実行
                cmd = [
                    "opengrok-indexer",
                    "--jar", "/opengrok/lib/opengrok.jar", "--",
                    "-c", "/usr/local/bin/ctags",
                    "-U", self.base_uri,
                    "-R", str(tmp_config_path),
                    "-H", project.name,
                ]
                subprocess.run(
                    cmd,
                    check=True,
                    stdin=subprocess.DEVNULL,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                )

                # opengrok-projadm --refreshを実行
                cmd = [
                    "opengrok-projadm",
                    "--jar", "/opengrok/lib/opengrok.jar",
                    "--base", "/opengrok/",
                    "--uri", self.base_uri,
                    "--refresh",
                ]
                subprocess.run(
                    cmd,
                    check=True,
                    stdin=subprocess.DEVNULL,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                )
            finally:
                tmp_config_path.unlink(missing_ok=True)

        # プロジェクトのメタデータをファイルに保存
        self.json_manager.save_project(project)

    def reindex_project(self, project: Project):
        """プロジェクトのインデックスを再生成"""
        cmd = [
            "opengrok-reindex-project",
            "-J=-Djava.util.logging.config.file=/opengrok/etc/logging.properties",
            "-a", "/opengrok/lib/opengrok.jar",
            "--uri", self.base_uri,
            "--printoutput",
            "-P", project.name,
            "--",
            "-c", "/usr/local/bin/ctags",
            "-H",
            # "-m", "10m",
            "-r", "dirbased",
            "--renamedHistory", "on",
            "--threads", str(os.cpu_count() or 1),
        ]
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr)
        result.check_returncode()

    def delete_project(self, project: Project):
        # opengrok-projadmコマンドでプロジェクトを削除
        cmd = [
            "opengrok-projadm",
            "--jar", "/opengrok/lib/opengrok.jar",
            "--base", "/opengrok/",
            "--uri", self.base_uri,
            "--delete", project.name,
        ]
        subprocess.run(
            cmd,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )

        # プロジェクトのメタデータファイルを削除
        self.json_manager.delete_project(project.name)

    def download_source_code(self, project: Project) -> bool:
        """プロジェクトのソースコードをダウンロード
        
        Returns:
            bool: 変更があった場合はTrue、変更がなかった場合はFalse
        """
        downloader = SourceCodeDownloader(self.src_dir, self.data_dir, self.json_manager)
        return downloader.download(project)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reindex-retries",
        type=int,
        default=3,
        help="Number of retries for reindex_project when it fails with subprocess.CalledProcessError",
    )
    args = parser.parse_args()

    expected_projects = ProjectDefsJson.parse(sys.stdin.read())

    # Directory layout:
    #  ${src_dir}/${project_name}/ - git worktree or content of archive file.
    #  ${data_dir}/${project_name}/project.json - project metadata.
    #  ${data_dir}/${project_name}/archive.${extension} - archive file.
    src_dir = pathlib.Path("/opengrok/src")
    data_dir = pathlib.Path("/opengrok/manager_data")
    client = OpenGrokClient('http://localhost:8080', src_dir, data_dir)
    actual_projects = client.get_projects()
    logger.info("get_projects", expected_projects=expected_projects, actual_projects=actual_projects)

    extra_project_names = set(actual_projects.keys()) - set(expected_projects.keys())
    for project_name in extra_project_names:
        client.delete_project(actual_projects[project_name])
        logger.info("Deleted project", name=project_name)

    for name in expected_projects:
        expected = expected_projects[name]
        actual = actual_projects.get(name)
        logger.info("Processing project", name=name, expected=expected, actual=actual)

        need_recreate = expected != actual and actual is not None
        if need_recreate:
            client.delete_project(actual)
            logger.info("Deleted project due to project.json mismatch", name=name)

        try:
            changed = client.download_source_code(expected)
            if not changed:
                logger.info("Project source code not changed", name=name)
                continue
        except Exception as e:
            logger.info("Failed to download source code", name=name, exception=str(e))
            continue

        logger.info("Project source code changed", name=name)
        if actual is None or need_recreate:
            logger.info("Adding project", name=name)
            client.add_project(expected)

        logger.info("Reindexing project", name=name)
        # NOTE: The opengrok-reindex-project command may fail with non-zero exit code. So we need to retry it.
        reindex_project_retry = tenacity.retry(
            retry=tenacity.retry_if_exception_type(subprocess.CalledProcessError),
            stop=tenacity.stop_after_attempt(args.reindex_retries),
            wait=tenacity.wait_exponential_jitter(max=60),
        )(client.reindex_project)
        reindex_project_retry(expected)  # type: ignore
        logger.info("Reindexed project", name=name)


if __name__ == "__main__":
    main()
