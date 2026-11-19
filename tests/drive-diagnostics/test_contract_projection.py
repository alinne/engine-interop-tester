"""Prove the route generator follows committed catalog authority and rejects drift."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('projection', ROOT / 'scripts/ops/Sync-DriveContracts.py')
projection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(projection)


class ContractProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_git('init', '-q')
        lines = []
        families = {'Cloud': [], 'CloudMedia': []}
        for index, (_, (family, member, operation_id)) in enumerate(projection.SELECTION.items()):
            method = 'POST' if index in (1, 4, 5) else 'GET'
            lines.append(f'Stable("{operation_id}", "{method}", EngineControlApiRoutes.{family}.{member}),')
            families[family].append(f'        public const string {member} = "/engine/v1/cloud/from-source-{index}";')
        self.catalog = self.root / projection.SOURCE_FILES[0]
        self.catalog.parent.mkdir(parents=True)
        self.catalog.write_text('\n'.join(lines))
        self.media = self.root / projection.SOURCE_FILES[1]
        self.media.write_text('\n'.join('public static class ' + family + '\n    {\n' + '\n'.join(members) + '\n    }' for family, members in families.items()))
        (self.root / projection.SOURCE_FILES[2]).write_text('// other canonical declarations')
        self.run_git('add', '.')
        self.run_git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')

    def run_git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.STDOUT)

    def test_projects_actual_declared_paths_and_methods(self):
        result = projection.project(self.root)
        self.assertEqual('/engine/v1/cloud/from-source-1', result['operations']['start']['path'])
        self.assertEqual('POST', result['operations']['start']['method'])
        self.assertEqual(self.run_git('rev-parse', 'HEAD').decode().strip(), result['sourceCommit'])
        self.assertEqual(3, len(result['sourceHashes']))

    def test_uncommitted_contract_changes_cannot_be_projected(self):
        self.media.write_text(self.media.read_text().replace('from-source-1', 'tampered'))
        with self.assertRaisesRegex(ValueError, 'uncommitted'):
            projection.project(self.root)

    def test_pinned_commit_is_independent_of_working_checkout(self):
        baseline = projection.project(self.root)
        self.media.write_text('unrelated uncommitted WIP')
        self.assertEqual(baseline, projection.project(self.root, baseline['sourceCommit']))

    def test_missing_catalog_binding_fails_closed(self):
        self.catalog.write_text(self.catalog.read_text().replace('cloud.assets.list', 'cloud.assets.removed'))
        self.run_git('add', '.')
        self.run_git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'removed operation')
        with self.assertRaisesRegex(ValueError, 'Missing canonical operation'):
            projection.project(self.root)

    def test_optional_drive_routes_are_projected_only_when_committed(self):
        self.catalog.write_text(self.catalog.read_text() + '\nInternal("cloud.media.browse", "POST", EngineControlApiRoutes.Cloud.MediaBrowse),')
        cloud = self.media.read_text().replace(
            '        public const string CollectionsList',
            '        public const string MediaBrowse = "/engine/v1/cloud/media:browse";\n        public const string CollectionsList')
        self.media.write_text(cloud)
        artifact = self.root / projection.OPTIONAL_SOURCE_FILES[0]
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text('')
        self.run_git('add', '.')
        self.run_git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'optional browse operation')
        result = projection.project(self.root)
        self.assertEqual('/engine/v1/cloud/media:browse', result['operations']['browse']['path'])
        self.assertEqual(4, len(result['sourceHashes']))

    def test_graph_routes_follow_committed_multiline_declarations(self):
        family = "ExternalMediaGraph"
        member = "PrepareBrowseTemplate"
        operation = "external-media.graph.prepare-browse"
        self.catalog.write_text(self.catalog.read_text() +
            f'\nInternal("{operation}", "POST", EngineControlApiRoutes.{family}.{member},')
        graph = self.root / projection.OPTIONAL_SOURCE_FILES[1]
        graph.write_text('public static class ExternalMediaGraph\n    {\n'
                         '        public const string PrepareBrowseTemplate =\n'
                         '            "/engine/v1/external-media/tenants/{tenantId}/prepare/browse";\n    }')
        self.run_git('add', '.')
        self.run_git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'graph operation')
        result = projection.project(self.root)
        self.assertEqual('POST', result['operations']['graphPrepareBrowse']['method'])
        self.assertEqual('/engine/v1/external-media/tenants/{tenantId}/prepare/browse',
                         result['operations']['graphPrepareBrowse']['path'])
        self.assertIn(str(graph.relative_to(self.root)), result['sourceHashes'])

    def test_graph_route_without_catalog_authority_is_rejected(self):
        graph = self.root / projection.OPTIONAL_SOURCE_FILES[1]
        graph.write_text('public static class ExternalMediaGraph\n    {\n'
                         '        public const string PrepareBrowseTemplate = "/unregistered";\n    }')
        self.run_git('add', '.')
        self.run_git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'unregistered graph operation')
        with self.assertRaisesRegex(ValueError, 'Missing canonical optional operation'):
            projection.project(self.root)
