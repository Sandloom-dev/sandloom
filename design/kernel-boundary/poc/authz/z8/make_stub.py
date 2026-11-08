"""Write the fixture's generated stub with the generator's own render_stub."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from sandloom.core.cli import dto_copies  # noqa: E402
from sandloom.core.cli.generated import render_stub  # noqa: E402
from z8provider import ProjectRoles  # noqa: E402

resolver = dto_copies.Resolver(
    provider_package="z8provider",
    dto_package="z8app.shared.contracts.projects_dto",
    shared_packages=("sandloom.shared", "z8app.shared"),
)
out = HERE / "app/z8app/shared/contracts/projects_roles_v1.py"
text = render_stub("projects.roles.v1", ProjectRoles, resolver)
out.write_text(dto_copies.ruff_formatted(text, out, cwd=HERE))
print(out.read_text())
