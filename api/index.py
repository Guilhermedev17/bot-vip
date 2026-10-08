"""Entrypoint da Vercel: importa o app Flask de app.py."""
from app import app  # noqa: F401  (a Vercel usa a variável `app`)
