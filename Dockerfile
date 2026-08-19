# NDARA — image de démonstration.
#
# Aucune dépendance à installer : le serveur et toute la chaîne statistique
# tiennent dans la bibliothèque standard. L'image ne fait que copier le code,
# semer une vague simulée reproductible, et lancer.
#
#   docker build -t ndara .
#   docker run -p 8000:7860 ndara      →  http://127.0.0.1:8000/

FROM python:3.12-slim

WORKDIR /app
COPY . .

# La vague de démonstration est semée AU MOMENT DE LA CONSTRUCTION, avec une
# graine figée : deux constructions successives donnent exactement les mêmes
# chiffres, et le tableau de bord n'est jamais vide devant un évaluateur.
# Ces entretiens portent le canal "simulation" et l'interface l'affiche.
RUN python scripts/simulate.py --n 500 --reset --seed 7

ENV PORT=7860
ENV HOST=0.0.0.0
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/health').read()"

CMD ["sh", "-c", "python web/server.py --host ${HOST} --port ${PORT}"]
