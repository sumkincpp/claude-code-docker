"""Feature manifests: loading, selection and rendering. Pure Python, no docker."""

import re
from pathlib import Path

import pytest

from ccd.features import BUNDLED_DIR, FeatureError, load_features, select, version_variable
from ccd.features.render import Renderer, RendererError, available_renderers, get_renderer


def write(root: Path, name: str, toml: str, assets: dict[str, str] | None = None) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "feature.toml").write_text(toml)
    for filename, content in (assets or {}).items():
        (directory / "assets").mkdir(exist_ok=True)
        (directory / "assets" / filename).write_text(content)


def load(root: Path):
    return load_features([(root, "test")])


def test_bundled_features_load_and_resolve_in_a_working_order():
    features = load_features([(BUNDLED_DIR, "bundled")])

    names = [f.name for f in select(features)]

    assert names.index("nvm") < names.index("node") < names.index("pnpm") < names.index("claude")
    assert "jules" not in names  # not a default
    assert {"claude", "codex", "gemini", "copilot", "opencode", "pi", "rust", "go", "uv", "local-claude"} <= set(names)


def test_bundled_flags_and_variables_follow_the_convention():
    features = load_features([(BUNDLED_DIR, "bundled")])

    assert features["claude"].versions == {"claude": "latest"}
    assert features["node"].versions == {"node": "24.15.0"}
    assert features["pnpm"].versions == {"pnpm": "latest", "npm": "latest"}
    assert features["rust"].versions == {"rustup": "latest"}
    assert features["local-claude"].versions == {}
    assert version_variable("local-claude") == "LOCAL_CLAUDE_VERSION"


@pytest.mark.parametrize("toml, message", [
    ('[install]\ncommand = "x"\nbogus = 1\n', "unknown [install] key"),
    ('colour = "red"\n[install]\ncommand = "x"\n', "unknown key"),
    ('description = "no install"\n', "[install] with a command is required"),
    ('[install]\ncommand = "a\\nb"\n', "single line"),
    ('[install]\ncommand = "x"\nuser = "bob"\n', "user must be"),
    ('requires = ["ghost"]\n[install]\ncommand = "x"\n', "requires unknown 'ghost'"),
    ('name = "Bad Name"\n[install]\ncommand = "x"\n', "invalid name"),
])
def test_invalid_manifests_are_rejected(tmp_path, toml, message):
    write(tmp_path, "thing", toml)

    with pytest.raises(FeatureError, match=re.escape(message)):
        load(tmp_path)


def test_bundled_agents_declare_their_config_mounts():
    features = load_features([(BUNDLED_DIR, "bundled")])

    mounts = {n: [(m.host, m.container, m.type) for m in f.mounts] for n, f in features.items() if f.mounts}

    assert mounts["claude"] == [("{home}/.claude", "~/.claude", "folder"), ("{home}/.claude.json", "~/.claude.json", "file")]
    assert set(mounts) == {"claude", "codex", "copilot", "gemini", "pi"}


def test_invalid_mounts_in_a_manifest_are_rejected(tmp_path):
    write(tmp_path, "thing", '[install]\ncommand = "x"\n[[mounts]]\nhost = "/a"\n')

    with pytest.raises(FeatureError, match="must have 'host' and 'container'"):
        load(tmp_path)


def test_dependency_cycles_and_duplicate_versions_are_rejected(tmp_path):
    write(tmp_path, "a", 'requires = ["b"]\n[install]\ncommand = "x"\n')
    write(tmp_path, "b", 'requires = ["a"]\n[install]\ncommand = "x"\n')
    with pytest.raises(FeatureError, match="dependency cycle"):
        load(tmp_path)

    other = tmp_path / "other"
    write(other, "x", '[versions]\nshared = "1"\n[install]\ncommand = "x"\n')
    write(other, "y", '[versions]\nshared = "2"\n[install]\ncommand = "x"\n')
    with pytest.raises(FeatureError, match="version 'shared' is declared by both"):
        load(other)


def test_capabilities_can_be_required_and_later_sources_override_by_name(tmp_path):
    base, user = tmp_path / "base", tmp_path / "user"
    write(base, "tool", 'provides = ["shell-tool"]\ndescription = "old"\n[install]\ncommand = "x"\n')
    write(base, "app", 'requires = ["shell-tool"]\n[install]\ncommand = "x"\n')
    write(user, "tool", 'provides = ["shell-tool"]\ndescription = "new"\n[install]\ncommand = "y"\n')

    features = load_features([(base, "bundled"), (user, "local")])

    assert features["tool"].description == "new" and features["tool"].source == "local"
    assert features["app"].requires == ("tool",)


def test_selection_adds_dependencies_and_orders_by_priority_then_name(tmp_path):
    write(tmp_path, "base", '[install]\ncommand = "x"\n')
    write(tmp_path, "zed", 'requires = ["base"]\n[install]\ncommand = "x"\n')
    write(tmp_path, "alpha", 'requires = ["base"]\n[install]\ncommand = "x"\n')
    write(tmp_path, "first", 'order = 1\n[install]\ncommand = "x"\n')
    features = load(tmp_path)

    assert [f.name for f in select(features)] == ["first", "base", "alpha", "zed"]
    assert [f.name for f in select(features, with_names={"zed"})] == ["base", "zed"]


def test_without_removes_a_default_but_not_a_needed_dependency(tmp_path):
    write(tmp_path, "base", '[install]\ncommand = "x"\n')
    write(tmp_path, "app", 'requires = ["base"]\n[install]\ncommand = "x"\n')
    write(tmp_path, "off", 'default = false\n[install]\ncommand = "x"\n')
    features = load(tmp_path)

    assert [f.name for f in select(features, without_names={"app"})] == ["base"]
    with pytest.raises(FeatureError, match="cannot exclude 'base': required by 'app'"):
        select(features, without_names={"base"})
    with pytest.raises(FeatureError, match="unknown feature"):
        select(features, with_names={"nope"})


def render(tmp_path, **features: str) -> str:
    for name, toml in features.items():
        write(tmp_path, name, toml)
    return get_renderer().render(select(load(tmp_path)))


def test_render_declares_each_arg_right_before_the_run_that_references_it(tmp_path):
    text = render(tmp_path, tool='[install]\ncommand = ["echo $TOOL_VERSION", "echo done"]\n', zother='[install]\ncommand = "echo other"\n')

    block = text[text.index("# feature: tool"):text.index("# feature: zother")]
    assert block.splitlines()[1] == "ARG TOOL_VERSION=latest"
    assert 'export TOOL_VERSION="${TOOL_VERSION}" && \\\n    echo $TOOL_VERSION && \\\n    echo done' in block
    assert "TOOL_VERSION" not in text[text.index("# feature: zother"):text.index("# Record what is installed")]


def test_render_records_every_feature_in_the_installed_list(tmp_path):
    text = render(tmp_path, tool='version = false\n[install]\ncommand = "echo 100%"\n[verify]\ncommand = "tool --version"\n')

    record = text[text.index("# Record what is installed"):]
    assert ": > /metadata/installed.jsonl" in record
    assert '"name": "tool"' in record and "echo 100%" in record
    assert '"$(tool --version 2>/dev/null | head -n1 |' in record


def test_render_orders_args_env_copy_and_run_and_declares_shared_args_once(tmp_path):
    provider = '[args]\nPOLICY = "7"\n[env]\nHOME_BIN = "/x/${PATH}"\n[[cache]]\nid = "c"\ntarget = "~/.c"\n[install]\ncommand = "true"\n'
    write(tmp_path, "provider", provider, assets={"tool.sh": "#!/bin/sh\n"})
    text = render(tmp_path, user='requires = ["provider"]\n[install]\ncommand = "true"\n')

    assert text.count("ARG POLICY=7") == 1
    block = text[text.index("# feature: provider"):text.index("# feature: user")]
    assert [line.split()[0] for line in block.splitlines() if line.startswith(("ARG", "ENV", "COPY", "RUN"))] == ["ARG", "ARG", "ENV", "COPY", "RUN"]
    assert 'ENV HOME_BIN="/x/${PATH}"' in block
    assert "COPY --chown=user:user features/provider/ /usr/local/share/ccd/features/provider/" in block
    assert 'export CCD_FEATURE_DIR="/usr/local/share/ccd/features/provider"' in block
    # the dependent inherits the cache mount and re-references the policy arg
    dependent = text[text.index("# feature: user"):]
    assert "--mount=type=cache,id=c,uid=1000,gid=1000,target=/home/user/.c" in dependent
    assert 'export POLICY="${POLICY}"' in dependent


def test_render_switches_user_for_root_installs_and_verifies_the_selection(tmp_path):
    text = render(
        tmp_path,
        sys='[install]\ncommand = "apk add x"\nuser = "root"\n[verify]\ncommand = "x --version"\n',
        plain='version = false\n[install]\ncommand = "true"\n[verify]\ncommand = ["a", "b"]\n',
    )

    assert "USER root\nRUN export SYS_VERSION" in text and "apk add x\nUSER user" in text
    assert "# Verify installations\nRUN a && \\\n    b && \\\n    x --version" in text
    assert "ARG PLAIN_VERSION" not in text


def test_disabled_features_leave_no_trace(tmp_path):
    features = load_features([(BUNDLED_DIR, "bundled")])

    text = get_renderer().render(select(features, without_names={"rust", "gemini"}))

    assert "rustup" not in text and "gemini" not in text.lower() and "GEMINI_VERSION" not in text
    assert "claude --version" in text and "WITH_" not in text


def test_stage_context_writes_dockerfile_core_files_and_assets(tmp_path):
    features = load_features([(BUNDLED_DIR, "bundled")])

    dockerfile = get_renderer().stage_context(select(features), tmp_path / "ctx")

    ctx = tmp_path / "ctx"
    assert dockerfile == ctx / "Dockerfile" and "COPY core/ccd-entrypoint" in dockerfile.read_text()
    assert (ctx / "core" / "ccd-entrypoint").is_file()
    assert (ctx / "features" / "pnpm" / "install-clis.mjs").is_file()
    assert (ctx / "features" / "local-claude" / "local-claude").is_file()


def _staged(paths: str, extra: str = "") -> str:
    return f'paths = {paths}\nversion = false\n{extra}[install]\ncommand = "true"\n'


@pytest.mark.parametrize("paths", ['["relative/x"]', '["/"]', '["~"]', '["/a b"]', '["/a*"]', '"/x"', '[""]'])
def test_paths_must_be_absolute_plain_paths_in_a_list(tmp_path, paths):
    write(tmp_path, "tool", f'paths = {paths}\n[install]\ncommand = "true"\n')

    with pytest.raises(FeatureError, match="paths"):
        load(tmp_path)


def test_paths_are_normalized_and_cannot_be_combined_with_root_installs(tmp_path):
    write(tmp_path, "tool", 'paths = ["~/.tool/", "~/.tool", "/opt/tool"]\n[install]\ncommand = "true"\n')
    assert load(tmp_path)["tool"].paths == ("~/.tool", "/opt/tool")

    write(tmp_path, "sys", 'paths = ["/usr/x"]\n[install]\ncommand = "apk add x"\nuser = "root"\n')
    with pytest.raises(FeatureError, match="root"):
        load(tmp_path)


def test_a_feature_without_paths_cannot_require_one_with_paths(tmp_path):
    write(tmp_path, "base-tool", _staged('["~/b"]'))
    write(tmp_path, "plain", 'requires = ["base-tool"]\n[install]\ncommand = "true"\n')

    with pytest.raises(FeatureError, match="'plain' has no paths.*'base-tool'"):
        load(tmp_path)


def test_staged_features_build_on_their_single_requirement_and_otherwise_on_pre(tmp_path):
    text = render(
        tmp_path,
        plain='version = false\n[install]\ncommand = "echo plain"\n',
        a=_staged('["~/a"]'),
        b=_staged('["~/b"]', 'requires = ["a"]\n'),
        c=_staged('["~/c"]'),
        d=_staged('["~/d"]', 'requires = ["b", "c"]\n'),
    )

    pre = text[text.index("FROM base AS pre"):text.index("# feature: a")]
    assert "echo plain" in pre and "AS f-" not in pre
    assert "FROM pre AS f-a" in text and "FROM f-a AS f-b" in text and "FROM pre AS f-c" in text
    # two requirements: nothing to build on, so what they left is copied in, a before b
    d = text[text.index("FROM pre AS f-d"):text.index("# Assemble the features")]
    copies = [line for line in d.splitlines() if line.startswith("COPY")]
    assert copies == [f"COPY --chown=user:user --from=f-{n} /home/user/{n} /home/user/{n}" for n in "abc"]


def test_the_final_stage_copies_every_path_checks_and_records(tmp_path):
    text = render(
        tmp_path,
        a=_staged('["~/a", "/opt/a"]', '[env]\nA_HOME = "/home/user/a"\n[verify]\ncommand = "a --version"\n'),
        b=_staged('["~/b"]', 'requires = ["a"]\n'),
    )

    final = text[text.index("FROM pre AS final"):]
    assert final.index('ENV A_HOME="/home/user/a"') < final.index("COPY --chown=user:user --from=f-a /home/user/a")
    assert "COPY --chown=user:user --from=f-a /opt/a /opt/a" in final
    assert final.index("--from=f-b") > final.index("--from=f-a")
    assert final.index("COPY --chown") < final.index("# Verify installations") < final.index("# Record what is installed") < final.index("COPY core/ccd-entrypoint")


def test_a_path_is_copied_from_the_feature_building_on_it_but_never_dropped_between_siblings(tmp_path):
    text = render(
        tmp_path,
        a=_staged('["~/shared"]'),
        b=_staged('["~/shared", "~/b"]', 'requires = ["a"]\n'),
        s1=_staged('["~/bin"]'),
        s2=_staged('["~/bin"]'),
    )

    final = text[text.index("FROM pre AS final"):]
    assert "--from=f-a /home/user/shared" not in final and "--from=f-b /home/user/shared" in final
    assert "--from=f-s1 /home/user/bin" in final and "--from=f-s2 /home/user/bin" in final


def test_stages_declare_the_args_they_use_because_args_do_not_carry_over(tmp_path):
    text = render(
        tmp_path,
        provider=_staged('["~/p"]', '[args]\nPOLICY = "7"\n'),
        user=_staged('["~/u"]', 'requires = ["provider"]\n'),
    )

    user = text[text.index("FROM f-provider AS f-user"):text.index("# Assemble the features")]
    assert user.index("ARG POLICY=7") < user.index('export POLICY="${POLICY}"')
    assert text[text.index("FROM pre AS final"):].count("ARG POLICY=7") == 1


def test_every_bundled_feature_declares_its_paths_and_the_default_build_chains_on_pnpm():
    features = load_features([(BUNDLED_DIR, "bundled")])
    assert [name for name, f in features.items() if not f.paths] == []

    text = get_renderer().render(select(features))

    assert "FROM f-nvm AS f-node" in text and "FROM f-node AS f-pnpm" in text and "FROM f-pnpm AS f-claude" in text
    final = text[text.index("FROM pre AS final"):]
    assert "--from=f-node" not in final and "--from=f-nvm" not in final  # f-pnpm holds ~/.nvm with its own changes
    assert final.count("--from=f-pnpm /home/user/.nvm ") == 1
    assert "/metadata/pnpm-cli-resolution" in final


def test_renderers_are_found_by_name_and_the_default_is_parallel():
    assert sorted(available_renderers()) == ["parallel", "sequential"]
    assert get_renderer().name == "parallel" and get_renderer("sequential").name == "sequential"

    with pytest.raises(RendererError, match="Invalid build mode 'fast'.*parallel, sequential"):
        get_renderer("fast")


def test_the_two_built_in_renderers_differ_only_in_how_features_are_laid_out():
    selected = select(load_features([(BUNDLED_DIR, "bundled")]), with_names={"claude"})

    parallel = get_renderer("parallel").render(selected)
    sequential = get_renderer("sequential").render(selected)

    assert "AS f-claude" in parallel and "AS f-" not in sequential
    for text in (parallel, sequential):
        assert "FROM base AS pre" in text and "FROM pre AS final" in text and "# Record what is installed" in text
    assert [line for line in sequential.splitlines() if line.startswith("# feature: ")] == [line for line in parallel.splitlines() if line.startswith("# feature: ")]


def test_a_renderer_can_be_added_through_the_entry_point_group(monkeypatch):
    class Mine(Renderer):
        name = "mine"
        description = "test renderer"

        def render(self, selected):
            return "mine: " + ",".join(f.name for f in selected)

        def stage_context(self, selected, destination):
            return destination

    class EntryPoint:
        name = "mine"

        def load(self):
            return Mine

    monkeypatch.setattr("ccd.features.render.entry_points", lambda group: [EntryPoint()])
    selected = select(load_features([(BUNDLED_DIR, "bundled")]), with_names={"go"})

    assert get_renderer("mine").render(selected) == "mine: go"
    assert sorted(available_renderers()) == ["mine", "parallel", "sequential"]
    with pytest.raises(RendererError, match="mine, parallel, sequential"):
        get_renderer("fast")
