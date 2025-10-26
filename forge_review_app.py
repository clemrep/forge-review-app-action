import os
import sys
import requests
import re
import time
import json

# --- Fonctions utilitaires ---

def get_input(name: str, default: str = None) -> str:
    """Récupère une variable d'environnement d'input de l'action."""
    value = os.environ.get(f"INPUT_{name.upper()}", default)
    # Gérer les inputs booléens qui pourraient être 'false'
    if value is None and default is None:
        # Vérifier si un input obligatoire est manquant
        is_required = name in ['forge_api_token', 'forge_server_id'] # A adapter si d'autres sont requis
        if is_required:
            print(f"Error: Required input '{name}' is missing.")
            sys.exit(1)
    return value

def to_bool(value: str) -> bool:
    """Convertit une chaîne en booléen."""
    if isinstance(value, str):
        return value.lower() in ('true', '1', 'yes')
    return bool(value)

def set_output(name: str, value: any):
    """Définit une variable de sortie pour l'action GitHub."""
    print(f"::set-output name={name}::{value}")

def slugify(text: str, separator: str = '-') -> str:
    """Convertit une chaîne en slug valide pour un nom d'hôte."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9]+', separator, text).strip(separator)
    return re.sub(r'[' + separator + ']{2,}', separator, text)

def db_slugify(text: str) -> str:
    """Convertit une chaîne en slug valide pour un nom de BDD."""
    text = text.lower()
    return re.sub(r'[^a-z0-9_]+', '_', text).strip('_')

# --- Classe de l'API Forge ---

class ForgeAPI:
    """Wrapper simple pour l'API Laravel Forge."""
    
    BASE_URL = "https://forge.laravel.com/api/v1"

    def __init__(self, token: str, server_id: str):
        if not token:
            raise ValueError("Token API Forge est requis.")
        if not server_id:
            raise ValueError("ID du serveur Forge est requis.")
        self.server_id = server_id
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        self.site_id = None # Sera défini après la création/recherche

    def _request(self, method: str, endpoint: str, data: dict = None, timeout: int = 30) -> dict:
        """Méthode générique pour les requêtes API."""
        url = f"{self.BASE_URL}{endpoint}"
        try:
            response = requests.request(method, url, headers=self.headers, json=data if data else None, timeout=timeout)
            
            response.raise_for_status() # Lève une exception pour les codes 4xx/5xx
            
            if response.status_code == 204 or not response.content:
                return {} # Pas de contenu (ex: DELETE)
            return response.json()

        except requests.exceptions.HTTPError as e:
            print(f"HTTP Error: {e.response.status_code} for {method} {url}")
            if e.response.text:
                print(f"Response: {e.response.text}")
            sys.exit(1)
        except requests.exceptions.RequestException as e:
            print(f"Request Error: {e}")
            sys.exit(1)

    def list_sites(self) -> list:
        return self._request("GET", f"/servers/{self.server_id}/sites").get("sites", [])

    def find_site_by_name(self, name: str) -> dict | None:
        for site in self.list_sites():
            if site.get("name") == name:
                return site
        return None

    def create_site(self, data: dict) -> dict:
        return self._request("POST", f"/servers/{self.server_id}/sites", data=data).get("site")

    def get_site(self, site_id: str) -> dict:
        return self._request("GET", f"/servers/{self.server_id}/sites/{site_id}").get("site")
    
    def wait_for_status(self, entity_type: str, entity_id: str, target_status: str = "installed", timeout: int = 300):
        """Sonde une ressource jusqu'à ce qu'elle atteigne le statut souhaité."""
        start_time = time.time()
        
        getter = None
        if entity_type == "site":
            getter = lambda: self.get_site(entity_id)
        elif entity_type == "database":
            getter = lambda: self._request("GET", f"/servers/{self.server_id}/databases/{entity_id}").get("database")
        elif entity_type == "ssl":
            getter = lambda: self._request("GET", f"/servers/{self.server_id}/sites/{self.site_id}/certificates/{entity_id}").get("certificate")
        elif entity_type == "worker":
             getter = lambda: self._request("GET", f"/servers/{self.server_id}/sites/{self.site_id}/workers/{entity_id}").get("worker")
        else:
            raise ValueError(f"Unknown entity type: {entity_type}")

        while True:
            if time.time() - start_time > timeout:
                raise TimeoutError(f"Timeout waiting for {entity_type} {entity_id} to be {target_status}.")
            
            resource = getter()
            if not resource:
                print(f"Warning: Could not fetch {entity_type} {entity_id}. Retrying...")
                time.sleep(10)
                continue

            status = resource.get("status")
            print(f"Waiting for {entity_type} {entity_id}... (current status: {status})")

            if status == target_status:
                print(f"✅ {entity_type.capitalize()} {entity_id} is now {target_status}.")
                return resource
            
            if status in ("failed", "installation_failed", "failed_installation"):
                raise Exception(f"❌ {entity_type.capitalize()} {entity_id} failed with status: {status}.")

            time.sleep(10) # Poll every 10 seconds

    def find_database_by_name(self, name: str) -> dict | None:
        dbs = self._request("GET", f"/servers/{self.server_id}/databases").get("databases", [])
        for db in dbs:
            if db.get("name") == name:
                return db
        return None
    
    def create_database(self, name: str, user: str) -> dict:
        data = {"name": name, "user": user}
        # L'API ne permet de lier un utilisateur que s'il existe.
        # Nous supposons que l'utilisateur 'forge' (ou 'database_user' fourni) existe.
        return self._request("POST", f"/servers/{self.server_id}/databases", data=data).get("database")

    def install_repository(self, site_id: str, data: dict) -> dict:
        return self._request("POST", f"/servers/{self.server_id}/sites/{site_id}/repository", data=data)

    def update_env_file(self, site_id: str, content: str):
        return self._request("POST", f"/servers/{self.server_id}/sites/{site_id}/env", data={"content": content})

    def update_deploy_script(self, site_id: str, content: str):
        return self._request("PUT", f"/servers/{self.server_id}/sites/{site_id}/deployment-script", data={"content": content})
    
    def get_ssl(self, site_id: str, domains: list) -> dict:
        return self._request("POST", f"/servers/{self.server_id}/sites/{site_id}/certificates/letsencrypt", data={"domains": domains}).get("certificate")

    def enable_quick_deploy(self, site_id: str, auto_source: bool):
        data = {"auto_source": auto_source}
        return self._request("POST", f"/servers/{self.server_id}/sites/{site_id}/deployment", data=data)

    def enable_horizon(self, site_id: str):
        return self._request("POST", f"/servers/{self.server_id}/sites/{site_id}/horizon", data={})

    def enable_scheduler(self, site_id: str):
        # L'API ne semble pas avoir de endpoint "scheduler", mais on peut créer une "Job"
        print("Scheduler enabling... Creating scheduled job for 'artisan schedule:run'")
        data = {
            "command": "schedule:run",
            "frequency": "minutely",
            "user": "forge" # A adapter si 'isolated' est True
        }
        return self._request("POST", f"/servers/{self.server_id}/jobs", data=data)

    def create_worker(self, site_id: str, data: dict) -> dict:
        return self._request("POST", f"/servers/{self.server_id}/sites/{site_id}/workers", data=data).get("worker")

    def deploy_site(self, site_id: str, timeout: int):
        print(f"Triggering deployment for site {site_id} (timeout: {timeout}s)...")
        # Le paramètre 'wait: true' fait que l'API attend la fin du déploiement
        return self._request("POST", f"/servers/{self.server_id}/sites/{site_id}/deployment/deploy", data={"wait": True}, timeout=timeout)

# --- Logique principale ---

def main():
    try:
        # --- 1. Récupérer les inputs et le contexte ---
        print("Parsing inputs and GitHub context...")
        
        token = get_input("forge_api_token")
        server_id = get_input("forge_server_id")
        
        # Contexte GitHub
        event_path = get_input("GITHUB_EVENT_PATH")
        repo_name = get_input("repository", get_input("GITHUB_REPOSITORY"))
        
        pr_number = None
        branch = get_input("branch")
        
        if not branch:
            branch = get_input("GITHUB_HEAD_REF") or get_input("GITHUB_REF_NAME")

        try:
            with open(event_path, 'r') as f:
                event = json.load(f)
            if 'pull_request' in event:
                pr_number = event['pull_request']['number']
                print(f"Detected Pull Request #{pr_number}.")
                if not get_input("branch"): # Prioriser la branche de la PR
                     branch = event['pull_request']['head']['ref']
        except Exception as e:
            print(f"Could not parse GITHUB_EVENT_PATH: {e}")

        print(f"Using branch: {branch}")
        if not branch:
            raise ValueError("Could not determine branch. Please set 'branch' input.")

        # --- 2. Déterminer les noms ---
        host_input = get_input("host")
        db_name_input = get_input("database_name")
        root_domain = get_input("root_domain")

        if host_input:
            host = host_input
        else:
            host_slug = slugify(branch)
            if to_bool(get_input("prefix_with_pr_number")) and pr_number:
                host = f"pr{pr_number}-{host_slug}"
            else:
                host = host_slug
                
            if root_domain:
                host = f"{host}.{root_domain}"
            if get_input("fqdn_prefix"):
                host = f"{get_input('fqdn_prefix')}{host}"

        if db_name_input:
            database_name = db_name_input
        else:
            db_slug = db_slugify(branch)
            if to_bool(get_input("prefix_with_pr_number")) and pr_number:
                database_name = f"pr{pr_number}_{db_slug}"
            else:
                database_name = db_slug
                
            if get_input("database_name_prefix"):
                database_name = f"{get_input('database_name_prefix')}{database_name}"
        
        # Limiter la longueur des noms de BDD (ex: 63 chars pour PostgreSQL)
        database_name = database_name[:63]

        print(f"Host name determined: {host}")
        print(f"Database name determined: {database_name}")
        
        # --- 3. Initialiser l'API ---
        api = ForgeAPI(token, server_id)
        
        # --- 4. Trouver ou Créer le Site ---
        site = api.find_site_by_name(host)
        if site:
            site_id = site['id']
            api.site_id = site_id
            print(f"Site '{host}' (ID: {site_id}) found. Checking status...")
            site = api.wait_for_status("site", site_id)
        else:
            print(f"Site '{host}' not found. Creating...")
            site_data = {
                "domain": host,
                "project_type": get_input("project_type"),
                "directory": get_input("directory"),
                "isolated": to_bool(get_input("isolated")),
                "php_version": get_input("php_version"),
            }
            if get_input("nginx_template"):
                site_data["nginx_template"] = int(get_input("nginx_template"))
            
            new_site = api.create_site(site_data)
            site_id = new_site['id']
            api.site_id = site_id
            print(f"Site created (ID: {site_id}). Waiting for installation...")
            site = api.wait_for_status("site", site_id)

        # --- 5. Trouver ou Créer la Base de Données ---
        db_pass = get_input("database_password")
        db_user = get_input("database_user", 'forge')
        
        if to_bool(get_input("create_database")):
            if not db_pass:
                raise ValueError("`database_password` est requis si `create_database` est 'true'.")
            
            db = api.find_database_by_name(database_name)
            if db:
                print(f"Database '{database_name}' found.")
            else:
                print(f"Database '{database_name}' not found. Creating and linking to user '{db_user}'...")
                new_db = api.create_database(database_name, db_user)
                print(f"Database created (ID: {new_db['id']}). Waiting for installation...")
                api.wait_for_status("database", new_db['id'])

        # --- 6. Configurer le Dépôt ---
        if to_bool(get_input("configure_repository")):
            if site.get("repository_status") != "installed":
                print("Configuring repository...")
                repo_data = {
                    "provider": get_input("repository_provider"),
                    "repository": repo_name,
                    "branch": branch,
                    "composer": to_bool(get_input("composer")),
                }
                api.install_repository(site_id, repo_data)
                print("Waiting for repository to install...")
                api.wait_for_status("site", site_id, target_status="installed")
            else:
                print("Repository already configured.")

        # --- 7. Mettre à jour les Stubs (.env et script de déploiement) ---
        print("Configuring stubs...")
        try:
            with open(get_input("env_stub_path"), 'r') as f:
                env_content = f.read()
            env_content = env_content.replace("STUB_HOST", host)
            env_content = env_content.replace("STUB_DATABASE_NAME", database_name)
            env_content = env_content.replace("STUB_DATABASE_USER", db_user)
            env_content = env_content.replace("STUB_DATABASE_PASSWORD", db_pass)
            api.update_env_file(site_id, env_content)
            print("✅ .env file updated.")
        except FileNotFoundError:
            print(f"Warning: env_stub_path '{get_input('env_stub_path')}' not found. Skipping .env update.")

        try:
            with open(get_input("deploy_script_stub_path"), 'r') as f:
                deploy_content = f.read()
            deploy_content = deploy_content.replace("STUB_HOST", host)
            api.update_deploy_script(site_id, deploy_content)
            print("✅ Deploy script updated.")
        except FileNotFoundError:
            print(f"Warning: deploy_script_stub_path '{get_input('deploy_script_stub_path')}' not found. Skipping deploy script update.")

        # --- 8. Configurer les options de déploiement ---
        if to_bool(get_input("quick_deploy_enabled")):
            print("Enabling Quick Deploy...")
            auto_source = to_bool(get_input("deployment_auto_source"))
            api.enable_quick_deploy(site_id, auto_source)
        
        if to_bool(get_input("horizon_enabled")):
            print("Enabling Horizon...")
            api.enable_horizon(site_id)
            
        if to_bool(get_input("scheduler_enabled")):
            print("Enabling Scheduler (via Job)...")
            api.enable_scheduler(site_id)
            
        # --- 9. Obtenir le certificat SSL ---
        if to_bool(get_input("letsencrypt_certificate")):
            if not site.get("is_secured"):
                print("Obtaining Let's Encrypt certificate...")
                all_domains = [host]
                aliases_input = get_input("aliases")
                if aliases_input:
                    for alias in aliases_input.split(','):
                        alias = alias.strip()
                        if not alias: continue
                        if root_domain:
                            all_domains.append(f"{alias}.{host}")
                        else:
                            all_domains.append(f"{alias}-{host}")
                
                print(f"Requesting certificate for domains: {all_domains}")
                cert_req = api.get_ssl(site_id, all_domains)
                print(f"Waiting for certificate (ID: {cert_req['id']}) to install...")
                ssl_timeout = int(get_input("certificate_setup_timeout", 120))
                api.wait_for_status("ssl", cert_req['id'], timeout=ssl_timeout)
            else:
                print("SSL already configured.")

        # --- 10. Créer un Worker (Optionnel) ---
        worker_id = None
        if to_bool(get_input("create_worker")):
            print("Creating worker...")
            worker_data = {
                "connection": get_input("worker_connection"),
                "timeout": int(get_input("worker_timeout", 90)),
                "sleep": int(get_input("worker_sleep", 60)),
                "processes": int(get_input("worker_processes", 1)),
                "stopwaitsecs": int(get_input("worker_stopwaitsecs", 600)),
                "daemon": to_bool(get_input("worker_daemon")),
                "force": to_bool(get_input("worker_force")),
                "php_version": get_input("worker_php_version", get_input("php_version"))
            }
            if get_input("worker_tries"):
                 worker_data["tries"] = int(get_input("worker_tries"))
            if get_input("worker_queue"):
                 worker_data["queue"] = get_input("worker_queue")

            new_worker = api.create_worker(site_id, worker_data)
            worker_id = new_worker['id']
            print(f"Worker created (ID: {worker_id}). Waiting for installation...")
            api.wait_for_status("worker", worker_id)
        
        # --- 11. Lancer le déploiement ---
        deploy_timeout = int(get_input("deployment_timeout", 900))
        deployment = api.deploy_site(site_id, deploy_timeout)
        
        print("\n✅ Deployment finished successfully!")
        if "output" in deployment:
            print("\n--- Deployment Output ---")
            print(deployment["output"])
            print("-------------------------\n")

        # --- 12. Définir les sorties ---
        set_output("host", host)
        set_output("database_name", database_name)
        set_output("site_id", site_id)
        if worker_id:
            set_output("worker_id", worker_id)

    except Exception as e:
        print(f"\n❌ An error occurred: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()


