from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent


class KeycloakDeploymentTest(unittest.TestCase):
  def test_optimized_production_image_is_version_pinned(self):
    dockerfile = (ROOT / "docker/keycloak/Dockerfile").read_text()
    self.assertEqual(dockerfile.count("quay.io/keycloak/keycloak:26.8.1"), 2)
    self.assertIn("kc.sh build", dockerfile)
    self.assertIn('CMD ["start", "--optimized"]', dockerfile)
    self.assertNotIn("start-dev", dockerfile)

  def test_network_exposure_and_persistence_are_explicit(self):
    compose = (ROOT / "docker/keycloak/compose.yaml").read_text()
    self.assertIn('"127.0.0.1:18080:8080"', compose)
    self.assertIn('"127.0.0.1:19000:9000"', compose)
    self.assertNotIn("5432:5432", compose)
    self.assertIn("internal: true", compose)
    self.assertIn("identity-postgres:/var/lib/postgresql/data", compose)
    self.assertIn("condition: service_healthy", compose)
    self.assertIn("/health/ready", compose)
    self.assertIn("${KEYCLOAK_DB_PASSWORD:?", compose)
    self.assertIn("${KEYCLOAK_ADMIN_PASSWORD:?", compose)
    self.assertNotIn("AI_AUDIT_DATABASE_URL", compose)

  def test_edge_proxy_overwrites_forwarding_headers(self):
    proxy = (ROOT / "docker/nginx/keycloak.conf.example").read_text()
    self.assertIn("proxy_pass http://127.0.0.1:18080;", proxy)
    self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", proxy)
    self.assertIn("proxy_set_header X-Forwarded-Proto https;", proxy)
    self.assertNotIn("19000", proxy)
    template = (ROOT / "docker/keycloak/deployment.env.example").read_text()
    self.assertIn("KEYCLOAK_DB_PASSWORD=\n", template)
    self.assertIn("KEYCLOAK_ADMIN_PASSWORD=\n", template)
