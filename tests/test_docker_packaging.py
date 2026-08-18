from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_docker_context_includes_required_data_manifests() -> None:
    dockerignore_lines = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "data" not in dockerignore_lines
    assert "data/" not in dockerignore_lines
    assert "data/*" in dockerignore_lines
    assert "!data/*.json" in dockerignore_lines

    required_manifests = {
        "achievements.json",
        "engagement_content.json",
        "pet_species.json",
        "profile_cosmetics.json",
        "roleplay_sfw.json",
    }
    assert required_manifests <= {path.name for path in (PROJECT_ROOT / "data").glob("*.json")}

    for dockerfile_name in ("Dockerfile", "Dockerfile.pterodactyl"):
        dockerfile = (PROJECT_ROOT / dockerfile_name).read_text(encoding="utf-8")
        assert "COPY data/*.json /app/catalog_data/" in dockerfile
