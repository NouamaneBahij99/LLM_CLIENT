# ========================================
# llm_service.py
# Fichier de logique applicative
# ========================================

import os
import torch
import redis
import json
import requests
from sentence_transformers import SentenceTransformer, util
from google import genai


# Importation des Configurations
from config import (
    REDIS_HOST, REDIS_PORT, REDIS_DB, OLLAMA_API_URL, 
    EMBEDDING_MODEL_NAME, SIMILARITY_THRESHOLD, OLLAMA_MODEL_NAME, 
    DATA_PATH, APPLICATIONS_IDS, API_KEY, DEVICE
)
from prompts import PROMPTS_BY_APPLICATIONS


# ========================================
# Initialisation des Services
# ========================================
try:
    # Création d'une connexion Redis globale, réutilisable (Pool de connexions implicite)
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
    r.ping()
except redis.exceptions.ConnectionError as e:
    print(f"❌ Erreur de connexion Redis : {e}")

try:

    GLOBAL_EMBEDDING_MODEL = SentenceTransformer(EMBEDDING_MODEL_NAME, device=DEVICE)
except Exception as e:
    print(f"❌ Erreur lors du chargement du modèle SentenceTransformer: {e}")

DATA = {}
EMBEDDINGS = None
TEXTS = None
PROMPT = None

# ========================================
# Fonctions de Service (Logique)
# ========================================

def load_application_data(app_id: int):
    """Charge les données RAG et le prompt spécifique à l'application."""
    global EMBEDDINGS, TEXTS, PROMPT
    
    if app_id not in APPLICATIONS_IDS:
        print(f"❌ Échec : L'ID {app_id} est inconnu.")
        return False

    application_name = APPLICATIONS_IDS[app_id]
    file_name = application_name.lower().replace("application_", "") + "_embeddings.pt"
    full_data_path = os.path.join(DATA_PATH, file_name)
    
    try:
        data_loaded = torch.load(
            full_data_path,
            map_location=torch.device(DEVICE)
            )
        
        EMBEDDINGS = data_loaded["embeddings"]
        TEXTS = data_loaded["texts"]
        PROMPT = PROMPTS_BY_APPLICATIONS[application_name]
        
        # print(f"✅ Application '{application_name}' initialisée. Données chargées : {full_data_path}")
        return True

    except FileNotFoundError:
        print(f"❌ Erreur : Fichier d'embeddings introuvable à {full_data_path}")
    except KeyError as e:
        print(f"❌ Erreur : Clé manquante dans les données/prompts : {e}")
    except Exception as e:
        print(f"❌ Erreur inconnue lors du chargement des données : {e}")
        
    return False


def get_embedding(text: str):
    """Utilise le modèle global chargé une seule fois."""
    return GLOBAL_EMBEDDING_MODEL.encode(text, convert_to_tensor=True)


def llm_response(prompt: str):
    """Appelle le service Ollama avec le modèle par défaut."""
    data = {
        "model": OLLAMA_MODEL_NAME,
        "prompt": prompt
    }
    response_text = ""
    response = requests.post(OLLAMA_API_URL, json=data, stream=True)
    
    for line in response.iter_lines():
        if line:
            text = json.loads(line)["response"]
            response_text += text  
    
    return response_text 
 
def gemini_response(prompt: str):
    client = genai.Client(api_key=API_KEY)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text


def prompt_formatter(query: str, context_items: list[str]) -> str:
    """Formate le prompt en utilisant la variable PROMPT chargée."""
    context = "- " + "\n- ".join(context_items) if context_items else "Aucune donnée disponible"
    
    if PROMPT is None:
        raise RuntimeError("Le prompt n'a pas été chargé. Appelez load_application_data d'abord.")
        
    base_prompt = PROMPT.format(
        context=context,
        query=query
    )
    # print(base_prompt)
    return base_prompt


def retrieve_relevant_resources(query: str, n_resources_to_return: int = 5):
    """Récupère les ressources pertinentes du RAG."""
    if EMBEDDINGS is None:
        raise RuntimeError("Les embeddings ne sont pas chargés.")

    query_embedding = GLOBAL_EMBEDDING_MODEL.encode(query, convert_to_tensor=True)

    dot_scores = util.dot_score(query_embedding, EMBEDDINGS)[0]

    scores, indices = torch.topk(dot_scores, k=n_resources_to_return)

    return indices.tolist()


def automate_asking(query: str):
    """Orchestre la récupération de contexte et l'appel LLM."""
    
    indices = retrieve_relevant_resources(query=query)
    context_items = [TEXTS[i] for i in indices]

    prompt = prompt_formatter(query=query, context_items=context_items)
    # return llm_response(prompt)
    return gemini_response(prompt)


def find_similar_question(query_emb, app_id):
    prefix = f"{app_id}:"

    for key in r.keys(f"{prefix}*"):
        raw = r.get(key)
        if not raw:
            continue  

        try:
            cached_data = json.loads(raw)
        except json.JSONDecodeError:
            continue  

        cached_emb = torch.tensor(cached_data["embedding"])
        similarity = util.cos_sim(query_emb, cached_emb).item()
        if similarity >= SIMILARITY_THRESHOLD:
            return key.decode(), cached_data["response"]

    return None, None

def ask_llm_with_redis_smart(question: str, app_id: str):
    query_emb = get_embedding(question)

    similar_key, cached_response = find_similar_question(query_emb, app_id)

    if cached_response:
        return cached_response, True

    response_text = automate_asking(question)

    cache_key = f"{app_id}:{question}"
    data_to_cache = {
        "embedding": query_emb.tolist(),
        "response": response_text
    }
    r.set(cache_key, json.dumps(data_to_cache))

    return response_text, False


# r.flushall()
