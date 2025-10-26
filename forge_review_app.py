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
    if value is None:
        # Gérer les inputs obligatoires qui seraient manquants
        if name in ['forge_api_token', 'forge_server_id', 'forge_organization']:
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
    output_file = os.environ.get('GITHUB_OUTPUT')
    if output_file:
        try:
            with open(output_file, 'a') as f:
                f.write(f"{name}={value}\n")
        except IOError as e:
            print(f"Error writing to GITHUB_OUTPUT: {e}")
            # Fallback à l'ancienne méthode
            print(f"::set-output name={name}::{value}")
    else:
        # Fallback pour les anciens runners
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
    
    BASE_URL = "https://forge.laravel.com/api"

    def __init__(self, token: str, organization: str, server_id: str):
        if not token:
            raise ValueError("Token API Forge est requis.")
        if not organization:
            raise ValueError("Organisation Forge est requise.")
        if not server_id:
            raise ValueError("ID du serveur Forge est requis.")
        self.organization = organization
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
            # CORRECTION : Gérer le 404 comme un 'None' (ressource non trouvée/prête)
            # Ne pas quitter le script, laisser la fonction appelante gérer cela.
            if e.response.status_code == 404:
                print(f"Warning: 404 Not Found for {method} {url}")
                return None # <--- NE PAS QUITTER, RETOURNER NONE
            
            # Pour les erreurs de validation (422), afficher les détails
            if e.response.status_code == 422:
                print(f"HTTP Error: 422 Validation Error for {method} {url}")
                if e.response.text:
                    print(f"Response: {e.response.text}")
                sys.exit(1)
                
            # Pour toutes les autres erreurs HTTP (500, 401, 403, etc.), imprimer et quitter.
            print(f"HTTP Error: {e.response.status_code} for {method} {url}")
            if e.response.text:
                print(f"Response: {e.response.text}")
            sys.exit(1) # <--- Quitter pour les erreurs inattendues
        except requests.exceptions.RequestException as e:
            print(f"Request Error: {e}")
            sys.exit(1) # <--- Quitter pour les erreurs de connexion

    def _extract_data(self, response: dict, key: str = None) -> any:
        """Extrait les données d'une réponse JSON:API."""
        if not response:
            return None
        
        if key and key in response:
            return response[key]
        
        # Structure JSON:API : data peut être un objet ou une liste
        if "data" in response:
            data = response["data"]
            # Si data est une liste, retourner la liste
            if isinstance(data, list):
                return data
            # Si data est un objet avec attributes, fusionner id et attributes
            if isinstance(data, dict) and "attributes" in data:
                # Fusionner l'ID avec les attributes
                result = data["attributes"].copy()
                if "id" in data:
                    result["id"] = data["id"]
                return result
            # Sinon retourner data tel quel
            return data
        
        return response

    def list_sites(self) -> list:
        response = self._request("GET", f"/orgs/{self.organization}/servers/{self.server_id}/sites")
        if not response:
            return []
        data = self._extract_data(response)
        # Si c'est une liste, retourner la liste
        if isinstance(data, list):
            # Extraire les attributes de chaque site dans la liste
            sites = []
            for item in data:
                if isinstance(item, dict) and "attributes" in item:
                    sites.append(item["attributes"])
                else:
                    sites.append(item)
            return sites
        return []

    def find_site_by_name(self, name: str) -> dict | None:
        """Recherche un site par son nom (peut être un sous-domaine ou un FQDN)."""
        # Extraire le sous-domaine si c'est un FQDN
        subdomain = name.split('.')[0] if '.' in name else name
        
        for site in self.list_sites():
            # Comparer le sous-domaine avec le champ "name"
            if site.get("name") == subdomain or site.get("name") == name:
                return site
            # Comparer avec l'URL complète si disponible
            if "url" in site and name in site.get("url", ""):
                return site
            # Comparer avec le domaine si disponible
            if site.get("domain") == name:
                return site
        return None

    def create_site(self, data: dict) -> dict:
        response = self._request("POST", f"/orgs/{self.organization}/servers/{self.server_id}/sites", data=data)
        if not response:
            return None
        data_extracted = self._extract_data(response)
        return data_extracted

    def get_site(self, site_id: str) -> dict:
        """Récupère un site, gère la réponse None de _request."""
        # CORRECTION : Gérer le cas où _request retourne None (à cause d'un 404)
        response = self._request("GET", f"/orgs/{self.organization}/servers/{self.server_id}/sites/{site_id}")
        if not response:
            return None
        return self._extract_data(response)
    
    def wait_for_status(self, entity_type: str, entity_id: str, target_status: str = "installed", timeout: int = 300):
        """Sonde une ressource jusqu'à ce qu'elle atteigne le statut souhaité."""
        start_time = time.time()
        
        getter = None
        # CORRECTION : Rendre les getters robustes au retour 'None' de _request
        if entity_type == "site":
            getter = lambda: self.get_site(entity_id)
        elif entity_type == "database":
            def get_db():
                res = self._request("GET", f"/orgs/{self.organization}/servers/{self.server_id}/databases/{entity_id}")
                return self._extract_data(res) if res else None
            getter = get_db
        elif entity_type == "ssl":
            def get_ssl_cert():
                res = self._request("GET", f"/orgs/{self.organization}/servers/{self.server_id}/sites/{self.site_id}/certificates/{entity_id}")
                return self._extract_data(res) if res else None
            getter = get_ssl_cert
        elif entity_type == "worker":
             def get_worker_status():
                res = self._request("GET", f"/orgs/{self.organization}/servers/{self.server_id}/sites/{self.site_id}/workers/{entity_id}")
                return self._extract_data(res) if res else None
             getter = get_worker_status
        else:
            raise ValueError(f"Unknown entity type: {entity_type}")

        while True:
            if time.time() - start_time > timeout:
                raise TimeoutError(f"Timeout waiting for {entity_type} {entity_id} to be {target_status}.")
            
            resource = getter()
            if not resource:
                # Cette condition est maintenant vraie si _request retourne None (404)
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
        response = self._request("GET", f"/orgs/{self.organization}/servers/{self.server_id}/databases")
        if not response:
            return []
        dbs_data = self._extract_data(response)
        dbs = dbs_data if isinstance(dbs_data, list) else []
        for db in dbs:
            # Extraire les attributes si nécessaire
            db_attrs = db.get("attributes", db) if isinstance(db, dict) else db
            if db_attrs.get("name") == name:
                return db_attrs
        return None
    
    def create_database(self, name: str, user: str) -> dict:
        data = {"name": name, "user": user}
        # Nous supposons que l'utilisateur 'forge' (ou 'database_user' fourni) existe.
        response = self._request("POST", f"/orgs/{self.organization}/servers/{self.server_id}/databases", data=data)
        return self._extract_data(response) if response else None

    def install_repository(self, site_id: str, data: dict) -> dict:
        return self._request("POST", f"/orgs/{self.organization}/servers/{self.server_id}/sites/{site_id}/repository", data=data)

    def update_env_file(self, site_id: str, content: str):
        return self._request("PUT", f"/orgs/{self.organization}/servers/{self.server_id}/sites/{site_id}/env", data={"content": content})

    def update_deploy_script(self, site_id: str, content: str):
        return self._request("PUT", f"/orgs/{self.organization}/servers/{self.server_id}/sites/{site_id}/deployment-script", data={"content": content})
    
    def get_ssl(self, site_id: str, domains: list) -> dict:
        response = self._request("POST", f"/orgs/{self.organization}/servers/{self.server_id}/sites/{site_id}/certificates/letsencrypt", data={"domains": domains})
        return self._extract_data(response) if response else None

    def enable_quick_deploy(self, site_id: str, auto_source: bool):
        data = {"auto_source": auto_source}
        return self._request("POST", f"/orgs/{self.organization}/servers/{self.server_id}/sites/{site_id}/deployment", data=data)

    def enable_horizon(self, site_id: str):
        return self._request("POST", f"/orgs/{self.organization}/servers/{self.server_id}/sites/{site_id}/horizon", data={})

    def enable_scheduler(self, site_id: str, user: str, site_name: str):
        print("Scheduler enabling... Creating scheduled job for 'artisan schedule:run'")
        
        command = f"php /home/{user}/{site_name}/current/artisan schedule:run"
        
        # Vérifier si le job existe déjà
        response = self._request("GET", f"/orgs/{self.organization}/servers/{self.server_id}/jobs")
        jobs_data = self._extract_data(response) if response else []
        jobs = jobs_data if isinstance(jobs_data, list) else []
        for job in jobs:
            job_attrs = job.get("attributes", job) if isinstance(job, dict) else job
            if job_attrs.get("command") == command and job_attrs.get("user") == user:
                print(f"Scheduler job (ID: {job_attrs.get('id')}) already exists.")
                return job_attrs

        print(f"Creating scheduler job with command: {command}")
        data = {
            "command": command,
            "frequency": "minutely",
            "user": user
        }
        return self._request("POST", f"/orgs/{self.organization}/servers/{self.server_id}/jobs", data=data)

    def create_worker(self, site_id: str, data: dict) -> dict:
        response = self._request("POST", f"/orgs/{self.organization}/servers/{self.server_id}/sites/{site_id}/workers", data=data)
        return self._extract_data(response) if response else None

    def deploy_site(self, site_id: str, timeout: int):
        print(f"Triggering deployment for site {site_id} (timeout: {timeout}s)...")
        # Le paramètre 'wait: true' fait que l'API attend la fin du déploiement
        return self._request("POST", f"/orgs/{self.organization}/servers/{self.server_id}/sites/{site_id}/deployment/deploy", data={"wait": True}, timeout=timeout)

# --- Logique principale ---

def main():
    try:
        # --- 1. Récupérer les inputs et le contexte ---
        print("Parsing inputs and GitHub context...")
        
        token = get_input("forge_api_token")
        server_id = get_input("forge_server_id")
        
        # Contexte GitHub (directement depuis l'environnement)
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        # Priorité : Input 'repository', sinon variable d'env GITHUB_REPOSITORY
        repo_name = get_input("repository") or os.environ.get("GITHUB_REPOSITORY")
        
        pr_number = None
        # Priorité : Input 'branch', sinon...
        branch = get_input("branch")
        
        if not branch:
            # Fallback sur les vars d'env si 'branch' n'est pas fourni
            branch = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME")

        if not event_path:
            raise ValueError("GITHUB_EVENT_PATH environment variable not found.")
            
        try:
            with open(event_path, 'r') as f:
                event = json.load(f)
            if 'pull_request' in event:
                pr_number = event['pull_request']['number']
                print(f"Detected Pull Request #{pr_number}.")
                # Si la branche n'était pas passée en input, et qu'on est sur une PR, GITHUB_HEAD_REF est le bon
                if not get_input("branch"):
                     branch = event['pull_request']['head']['ref']
        except Exception as e:
            print(f"Could not parse GITHUB_EVENT_PATH: {e}")

        print(f"Using branch: {branch}")
        if not branch:
            raise ValueError("Could not determine branch. Please set 'branch' input or ensure GITHUB_HEAD_REF/GITHUB_REF_NAME are available.")

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
        organization = get_input("forge_organization")
        api = ForgeAPI(token, organization, server_id)
        
        # --- 4. Trouver ou Créer le Site ---
        # Extraire le sous-domaine sans le root_domain pour le champ "name"
        site_name = host.split('.')[0] if '.' in host else host
        
        print(f"Searching for site with host: {host}, subdomain: {site_name}")
        site = api.find_site_by_name(host)
        is_isolated = to_bool(get_input("isolated"))
        
        if site:
            site_id = site.get('id') or site.get('attributes', {}).get('id')
            api.site_id = site_id
            print(f"Site '{host}' (ID: {site_id}) found. Checking status...")
            site = api.wait_for_status("site", site_id)
        else:
            print(f"Site '{host}' not found. Creating...")
            site_data = {
                "type": get_input("project_type"),  # Doit être un des types valides : laravel, symfony, etc.
                "name": site_name,  # Sous-domaine uniquement (sans points)
                "domain_mode": "subdomain",  # Obligatoire
                "php_version": get_input("php_version"),
                "is_isolated": is_isolated,
            }
            
            # Ajouter web_directory si spécifié
            web_dir = get_input("directory")
            if web_dir:
                site_data["web_directory"] = web_dir
            
            # Ajouter nginx_template_id si spécifié
            if get_input("nginx_template"):
                site_data["nginx_template_id"] = int(get_input("nginx_template"))
            
            new_site = api.create_site(site_data)
            site_id = new_site.get('id') or new_site.get('attributes', {}).get('id')
            api.site_id = site_id
            print(f"Site created (ID: {site_id}). Waiting for installation...")
            site = api.wait_for_status("site", site_id)

        # Déterminer l'utilisateur du site
        site_user = site.get("user", "forge")
        
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
                db_id = new_db.get('id') or new_db.get('attributes', {}).get('id')
                print(f"Database created (ID: {db_id}). Waiting for installation...")
                api.wait_for_status("database", db_id)

        # --- 6. Configurer le Dépôt ---
        if to_bool(get_input("configure_repository")):
            repo_data = {
                "provider": get_input("repository_provider"),
                "repository": repo_name,
                "branch": branch,
                "composer": to_bool(get_input("composer")),
            }
            
            needs_repo_install = False
            repo_info = site.get("repository", {})
            if not isinstance(repo_info, dict):
                repo_info = {}
            
            if repo_info.get("status") != "installed":
                print("Repository not installed. Configuring...")
                needs_repo_install = True
            elif repo_info.get("branch") != branch:
                print(f"Branch mismatch. Site is on '{repo_info.get('branch')}', changing to '{branch}'.")
                needs_repo_install = True
            elif repo_info.get("url") != repo_name:
                 print(f"Repository mismatch. Site is on '{repo_info.get('url')}', changing to '{repo_name}'.")
                 needs_repo_install = True

            if needs_repo_install:
                api.install_repository(site_id, repo_data)
                print("Waiting for repository to install...")
                api.wait_for_status("site", site_id, target_status="installed")
            else:
                print("Repository already configured and up-to-date.")

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
            api.enable_scheduler(site_id, site_user, host)
            
        # --- 9. Obtenir le certificat SSL ---
        if to_bool(get_input("letsencrypt_certificate")):
            # Construire la liste complète des domaines attendus
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
            
            needs_ssl_update = False
            if not site.get("https"):
                print("Site is not secured. Requesting SSL.")
                needs_ssl_update = True
            else:
                # Le site est sécurisé, vérifier si les domaines correspondent
                print("Site is secured. Checking domains...")
                certs_response = api._request("GET", f"/orgs/{api.organization}/servers/{server_id}/sites/{site_id}/certificates")
                existing_certs = []
                if certs_response:
                    certs_data = api._extract_data(certs_response)
                    existing_certs = certs_data if isinstance(certs_data, list) else []
                active_cert_domains = []
                if existing_certs:
                    # Trouver le certificat actif (ou le premier)
                    for cert in existing_certs:
                        cert_attrs = cert.get("attributes", cert) if isinstance(cert, dict) else cert
                        if cert_attrs.get("status") == "installed":
                            active_cert_domains = cert_attrs.get("domains", [])
                            break
                    if not active_cert_domains and existing_certs:
                         first_cert_attrs = existing_certs[0].get("attributes", existing_certs[0]) if isinstance(existing_certs[0], dict) else existing_certs[0]
                         active_cert_domains = first_cert_attrs.get("domains", [])
                
                if set(active_cert_domains) != set(all_domains):
                    print(f"SSL domains mismatch. Requesting update.")
                    print(f"  Expected: {set(all_domains)}")
                    print(f"  Found: {set(active_cert_domains)}")
                    needs_ssl_update = True
                else:
                    print("SSL domains match. No update needed.")

            if needs_ssl_update:
                print(f"Requesting certificate for domains: {all_domains}")
                cert_req = api.get_ssl(site_id, all_domains)
                cert_id = cert_req.get('id') or cert_req.get('attributes', {}).get('id')
                print(f"Waiting for certificate (ID: {cert_id}) to install...")
                ssl_timeout = int(get_input("certificate_setup_timeout", 120))
                api.wait_for_status("ssl", cert_id, timeout=ssl_timeout)
            else:
                print("SSL already configured and domains match.")
        else:
            print("Skipping SSL setup (letsencrypt_certificate is 'false').")

        # --- 10. Créer un Worker (Optionnel) ---
        worker_id = None
        if to_bool(get_input("create_worker")):
            print("Checking for worker...")
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

            # Vérifier si un worker similaire existe déjà
            workers_response = api._request("GET", f"/orgs/{api.organization}/servers/{server_id}/sites/{site_id}/workers")
            existing_workers = []
            if workers_response:
                workers_data = api._extract_data(workers_response)
                existing_workers = workers_data if isinstance(workers_data, list) else []
            found_worker = None
            for w in existing_workers:
                # Extraire les attributes si nécessaire
                w_attrs = w.get("attributes", w) if isinstance(w, dict) else w
                # Simple vérification (peut être affinée)
                if (w_attrs.get("connection") == worker_data["connection"] and 
                    w_attrs.get("queue") == worker_data.get("queue") and
                    w_attrs.get("status") == "installed"):
                    print(f"Worker (ID: {w_attrs.get('id')}) already exists. Skipping creation.")
                    found_worker = w_attrs
                    worker_id = w_attrs.get('id')
                    break
            
            if not found_worker:
                print("Creating new worker...")
                new_worker = api.create_worker(site_id, worker_data)
                worker_id = new_worker.get('id') or new_worker.get('attributes', {}).get('id')
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

