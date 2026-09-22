"""LLM configuration and filesystem paths."""
from langchain_ollama.chat_models import ChatOllama

# ---------------------------------------------------------------- Paths
chats_dir = r"C:\Users\Grace\Documents\Code\DATA_Summer_2025\pdf\New_studies_70"
pdf_dir = r"C:\Users\Grace\Documents\Code\DATA_Summer_2025\pdf\New_studies_70"
handbook_dir = r".\dependants\eau1-handbook-structured.pdf"
embedding_model_fp = r".\embeddings_local\all-MiniLM-L6-v2"

run_number = 1

# ---------------------------------------------------------------- LLM
llm = ChatOllama(
    model='phi4',
    num_ctx=16000,
    temperature=0.7,
    verbose=False,
    num_gpu=1,  # number of gpus to use
    num_predict=500,
    repeat_penalty=1.2,
    top_k=50,
    top_p=0.85,
)
