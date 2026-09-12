"""
Plateforme de scouting Charleroi -- v1 (recherche, filtres, fiche joueur).
===============================================================================
Lit UNIQUEMENT la base DuckDB construite par db_build.py. Aucun calcul de
score ici : la formule reste dans Charleroi_MultiPoste_ScoreV8.py, la base en
contient le resultat et ses composants. La plateforme ne doit jamais
recalculer un score a sa facon (cf. bug de calibration des vieux scripts ML).

LANCEMENT :
    streamlit run impect-scouting/app.py
    (depuis la racine du projet, ou charleroi_scouting.duckdb est situe)
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st

_ICI = Path(__file__).resolve().parent
# A cote de app.py une fois deployee, a la racine du projet en local.
DB = next((p for p in (_ICI / "charleroi_scouting.duckdb",
                       _ICI.parent / "charleroi_scouting.duckdb") if p.exists()),
          _ICI / "charleroi_scouting.duckdb")
ARCHETYPES = {"CB": "Défenseur central", "FB": "Latéral", "WG": "Ailier",
              "SIX": "Milieu défensif (6)", "EIGHT": "Milieu relayeur (8)",
              "TEN": "Meneur offensif (10)", "NINE": "Avant-centre (9)"}

st.set_page_config(page_title="Scouting Impect — Charleroi", page_icon="🦓", layout="wide")


# ----------------------------------------------------------------- acces
# Comptes definis dans .streamlit/secrets.toml (mots de passe en SHA-256,
# jamais en clair). Une connexion par session de navigateur.
def _comptes() -> dict:
    """{} si aucun secrets.toml n'existe encore (st.secrets leve sinon)."""
    try:
        return dict(st.secrets.get("utilisateurs", {}))
    except Exception:
        return {}


def _mot_de_passe_ok(utilisateur: str, mot_de_passe: str) -> bool:
    attendu = _comptes().get(utilisateur)
    return bool(attendu) and hmac.compare_digest(
        str(attendu), hashlib.sha256(mot_de_passe.encode()).hexdigest())


def authentifier() -> None:
    if st.session_state.get("connecte"):
        return
    st.title("🦓 Scouting Impect — Charleroi")
    if not _comptes():
        st.error("Aucun compte configuré. Lance :\n\n"
                 "`python impect-scouting/gerer_comptes.py`")
        st.stop()
    with st.form("connexion"):
        u = st.text_input("Utilisateur")
        m = st.text_input("Mot de passe", type="password")
        if st.form_submit_button("Se connecter"):
            if _mot_de_passe_ok(u, m):
                st.session_state["connecte"] = u
                st.rerun()
            st.error("Identifiants incorrects.")
    st.stop()


authentifier()


@st.cache_resource
def connexion():
    if not DB.exists():
        st.error(f"Base introuvable : {DB}\n\nLance d'abord : python impect-scouting/db_build.py")
        st.stop()
    return duckdb.connect(str(DB), read_only=True)


@st.cache_data(ttl=600)
def requete(sql: str, params: tuple = ()) -> pd.DataFrame:
    return connexion().execute(sql, params).df()


@st.cache_data(ttl=600)
def listes():
    comp = requete("""SELECT competition, pays, niveau, top5_europe
                      FROM dim_competition ORDER BY rating_moyen DESC""")
    saisons = requete("SELECT DISTINCT season FROM dim_saison ORDER BY season DESC")["season"].tolist()
    maj = requete("SELECT genere_le FROM parametres")["genere_le"].iloc[0]
    return comp, saisons, maj


comp_df, saisons, genere_le = listes()

# ----------------------------------------------------------------- filtres
st.sidebar.title("🦓 Scouting Impect")
st.sidebar.caption(f"Données calculées le {genere_le}")
st.sidebar.caption(f"Connecté : {st.session_state['connecte']}")
if st.sidebar.button("Se déconnecter"):
    st.session_state.clear(); st.rerun()

# Libelle inclus dans l'option (plutot que format_func) : identique a l'ecran,
# mais pilotable par les tests automatises de Streamlit.
archetype = st.sidebar.selectbox(
    "Poste / archétype", [f"{a} — {n}" for a, n in ARCHETYPES.items()]).split(" — ")[0]
saison = st.sidebar.multiselect("Saison", saisons, default=[s for s in saisons if s in ("25/26", "2026")])
recherche = st.sidebar.text_input("Recherche par nom", placeholder="ex. Beitia")

st.sidebar.markdown("**Championnats**")
exclure_top5 = st.sidebar.checkbox("Exclure les 5 grands championnats", value=False)
pays_dispo = sorted([p for p in comp_df["pays"].dropna().unique()])
pays = st.sidebar.multiselect("Pays", pays_dispo)
niveaux = st.sidebar.multiselect("Niveau de division", [1, 2, 3, 4, 5],
                                 help="1 = première division. Vide = tous, y compris les non renseignés.")

# ------------------------------------------------------- shortlists / exclusions
# Base separee, en ecriture : la base de scoring reste ouverte en lecture seule
# et est ecrasee a chaque db_build.py, les listes doivent lui survivre.
# Surchargeable par la variable d'environnement SCOUTING_LISTES_DB : les tests
# automatises ecrivent ainsi dans un fichier temporaire, jamais dans le tien.
LISTES_DB = Path(os.environ.get("SCOUTING_LISTES_DB", DB.parent / "shortlists.duckdb"))


@st.cache_resource
def con_listes():
    c = duckdb.connect(str(LISTES_DB))
    c.execute("""CREATE TABLE IF NOT EXISTS listes (
        liste VARCHAR, playerId BIGINT, nom VARCHAR, club VARCHAR, poste VARCHAR,
        archetype VARCHAR, score DOUBLE, ajoute_le TIMESTAMP)""")
    return c


def listes_noms() -> list[str]:
    return [r[0] for r in con_listes().execute(
        "SELECT DISTINCT liste FROM listes WHERE liste <> 'exclus' ORDER BY liste").fetchall()]


def contenu(liste: str) -> pd.DataFrame:
    return con_listes().execute(
        "SELECT nom, club, poste, archetype, score, playerId FROM listes WHERE liste=? ORDER BY score DESC",
        [liste]).df()


def ids_exclus() -> list[int]:
    return [r[0] for r in con_listes().execute(
        "SELECT DISTINCT playerId FROM listes WHERE liste='exclus'").fetchall()]


def ajouter(liste: str, j) -> None:
    con_listes().execute("DELETE FROM listes WHERE liste=? AND playerId=?", [liste, int(j.playerId)])
    con_listes().execute("INSERT INTO listes VALUES (?,?,?,?,?,?,?,now())",
                         [liste, int(j.playerId), j.nom, j.club, j.poste, j.archetype_courant, float(j.score)])


def retirer(liste: str, player_id: int) -> None:
    con_listes().execute("DELETE FROM listes WHERE liste=? AND playerId=?", [liste, int(player_id)])


st.sidebar.markdown("**Listes**")
_noms = listes_noms()
shortlist = st.sidebar.selectbox("Shortlist active", _noms + ["➕ nouvelle liste…"],
                                 index=0 if _noms else len(_noms))
if shortlist == "➕ nouvelle liste…":
    shortlist = st.sidebar.text_input("Nom de la nouvelle liste", value="Shortlist") or "Shortlist"
masquer_exclus = st.sidebar.checkbox("Masquer les joueurs exclus", value=True)

st.sidebar.markdown("**Profil**")
age_max = st.sidebar.slider("Âge maximum", 16, 40, 40)
minutes_min = st.sidebar.slider("Minutes minimum", 400, 3000, 900, step=100)
pieds = st.sidebar.multiselect("Pied fort", ["droit", "gauche", "les deux"])
score_min = st.sidebar.slider("Score minimum", 0, 100, 0, step=5)

where, params = ["archetype = ?", "minutes_jouees >= ?", "age_years <= ?", "score >= ?"], \
                [archetype, minutes_min, age_max, score_min]
if saison:
    where.append(f"saison IN ({','.join('?' * len(saison))})"); params += saison
if recherche:
    where.append("lower(nom) LIKE ?"); params.append(f"%{recherche.lower()}%")
if exclure_top5:
    where.append("NOT top5_europe")
if pays:
    where.append(f"pays IN ({','.join('?' * len(pays))})"); params += pays
if niveaux:
    where.append(f"niveau IN ({','.join('?' * len(niveaux))})"); params += [float(n) for n in niveaux]
if pieds:
    where.append(f"pied_fort IN ({','.join('?' * len(pieds))})"); params += pieds
_exclus = ids_exclus()
if masquer_exclus and _exclus:
    where.append(f"playerId NOT IN ({','.join('?' * len(_exclus))})"); params += _exclus

res = requete(f"""
    SELECT nom, club, competition, pays, niveau, saison, round(score,1) AS score,
           round(age_years,1) AS age, minutes_jouees AS minutes, pied_fort,
           round(score_performance,1) AS performance, round(ajust_niveau,1) AS aj_niveau,
           round(ajust_age,1) AS aj_age, round(progression_credible,1) AS progression,
           round(gros_matchs_percentile,0) AS gros_matchs, round(opp_coef_avg,3) AS coef_adv,
           rang_archetype AS rang_mondial, round(score_percentile,1) AS percentile,
           round(base,1) AS base, round(excellence,1) AS excellence,
           round(fragilite,1) AS fragilite, pilier_fort, pilier_faible, taille_cm,
           n_matches_oppw AS matchs, position AS poste, side,
           round(attdef_coef_att_avg,3) AS coef_att, round(attdef_coef_def_avg,3) AS coef_def,
           round(club_rating,3) AS rating_club, round(competition_avg_rating,3) AS rating_ligue,
           round(gros_matchs_delta,1) AS gros_matchs_delta,
           playerId, squadId, iterationId, position
    FROM v_joueurs WHERE {' AND '.join(where)}
    ORDER BY score DESC LIMIT 500""", tuple(params))

# ----------------------------------------------------------------- fiche joueur
n_ = lambda v, f="{:.1f}", d="—": (f.format(v) if pd.notna(v) else d)


def carte_joueur(j) -> None:
    """Fiche complete d'un joueur. j doit porter une colonne archetype_courant."""
    arch = j.archetype_courant
    cle = (int(j.playerId), int(j.squadId), int(j.iterationId), j.position, arch)

    st.subheader(j.nom)
    lieu = f"{j.club} · {j.competition}"
    if pd.notna(j.pays):
        lieu += f" ({j.pays}" + (f", D{int(j.niveau)})" if pd.notna(j.niveau) else ")")
    st.caption(f"{lieu} · {j.saison} · {j.poste}" + (f" ({j.side})" if pd.notna(j.side) else "")
               + f" · archétype {arch}")

    k_ = st.columns(6)
    k_[0].metric("Score", n_(j.score), f"rang mondial {int(j.rang_mondial)}")
    k_[1].metric("Performance", n_(j.performance), f"percentile {n_(j.percentile, '{:.0f}')}")
    k_[2].metric("Âge", n_(j.age))
    k_[3].metric("Minutes", n_(j.minutes, "{:.0f}"), f"{n_(j.matchs, '{:.0f}')} matchs")
    k_[4].metric("Pied", j.pied_fort or "—")
    k_[5].metric("Taille", n_(j.taille_cm, "{:.0f} cm"))

    st.markdown(
        f"**Score {n_(j.score)}** = performance {n_(j.performance)} "
        f"({n_(j.base)} de base {j.excellence:+.1f} excellence {-j.fragilite:+.1f} fragilité) "
        f"{j.aj_niveau:+.1f} niveau {j.aj_age:+.1f} âge")

    b1, b2, b3, b4 = st.columns([1, 1, 1, 2])
    if b1.button(f"➕ Ajouter à « {shortlist} »", width="stretch", key=f"add_{cle}"):
        ajouter(shortlist, j); st.toast(f"{j.nom} ajouté à « {shortlist} »"); st.rerun()
    if b2.button("🚫 Exclure ce joueur", width="stretch", key=f"exc_{cle}",
                 help="Il ne sera plus proposé dans les recherches par poste."):
        ajouter("exclus", j); st.toast(f"{j.nom} exclu"); st.rerun()
    if b3.button(f"🏟️ Effectif de {j.club}", width="stretch", key=f"clu_{cle}"):
        st.query_params["club"] = f"{int(j.squadId)}-{int(j.iterationId)}"
        st.rerun()

    g1, g2 = st.columns([3, 2])
    with g1:
        piliers = requete("""SELECT pilier, percentile, poids FROM fait_pilier
            WHERE playerId=? AND squadId=? AND iterationId=? AND position=? AND archetype=?
            ORDER BY poids DESC""", cle)
        piliers["pilier"] = piliers["pilier"].str.replace("_", " ")
        fig = px.bar(piliers, x="percentile", y="pilier", orientation="h",
                     range_x=[0, 100], text="percentile",
                     labels={"percentile": "Percentile du poste", "pilier": ""},
                     hover_data={"poids": ":.1%"})
        fig.update_traces(texttemplate="%{text:.0f}", textposition="outside",
                          marker_color="#1F6F6B", cliponaxis=False)
        fig.update_layout(height=360, margin=dict(l=0, r=20, t=30, b=0),
                          yaxis=dict(autorange="reversed"),
                          title="Piliers — survol : poids dans le score")
        fig.add_vline(x=50, line_dash="dot", line_color="#999")
        st.plotly_chart(fig, width="stretch", key=f"fig_{cle}")

    with g2:
        st.markdown("**Contexte de match**")
        st.dataframe(pd.DataFrame({
            "indicateur": ["Coefficient adversaire", "Coef att/def (attaque)",
                           "Coef att/def (défense)", "Rating du club",
                           "Rating moyen du championnat"],
            "valeur": [n_(j.coef_adv, "{:.3f}"), n_(j.coef_att, "{:.3f}"),
                       n_(j.coef_def, "{:.3f}"), n_(j.rating_club, "{:.3f}"),
                       n_(j.rating_ligue, "{:.3f}")]}),
            hide_index=True, width="stretch")
        forme = []
        if pd.notna(j.progression):
            forme.append(f"- Progression crédible : **{j.progression:+.1f}** point")
        if pd.notna(j.gros_matchs):
            forme.append(f"- Gros matchs : **{j.gros_matchs:.0f}e percentile** "
                         f"(delta {j.gros_matchs_delta:+.1f})")
        forme.append(f"- Pilier le plus fort : **{str(j.pilier_fort).replace('_', ' ')}**")
        forme.append(f"- Pilier le plus faible : **{str(j.pilier_faible).replace('_', ' ')}**")
        st.markdown("**Forme et gros matchs**\n" + "\n".join(forme))

    met = requete("""SELECT metrique, pilier, valeur_brute, z FROM fait_metrique
        WHERE playerId=? AND squadId=? AND iterationId=? AND position=? AND archetype=?
        ORDER BY z DESC""", cle)
    if not met.empty:
        f1, f2 = st.columns(2)
        f1.markdown("**Points forts**")
        f1.dataframe(met.head(7)[["metrique", "pilier", "z", "valeur_brute"]].round(2),
                     hide_index=True, width="stretch")
        f2.markdown("**Points faibles**")
        f2.dataframe(met.tail(7)[["metrique", "pilier", "z", "valeur_brute"]].round(2).iloc[::-1],
                     hide_index=True, width="stretch")

    a1, a2 = st.columns(2)
    autres = requete("""SELECT archetype, round(score,1) AS score, rang_archetype AS rang
        FROM fait_joueur_saison
        WHERE playerId=? AND squadId=? AND iterationId=? AND position=? AND archetype<>?
        ORDER BY score DESC""", cle)
    with a1:
        st.markdown("**Autres archétypes de ce poste**")
        if not autres.empty:
            st.dataframe(autres, hide_index=True, width="stretch")
        else:
            st.caption("Ce poste ne correspond qu'à un seul archétype.")
    hist = requete("""SELECT saison, competition, round(score,1) AS score, minutes_jouees AS minutes
        FROM v_joueurs WHERE playerId=? AND archetype=? ORDER BY saison""",
                   (int(j.playerId), arch))
    with a2:
        st.markdown("**Historique du joueur**")
        if len(hist) > 1:
            st.dataframe(hist, hide_index=True, width="stretch")
        else:
            st.caption("Une seule saison disponible pour ce joueur.")


CHAMPS = """nom, club, competition, pays, niveau, saison, round(score,1) AS score,
    round(age_years,1) AS age, minutes_jouees AS minutes, pied_fort,
    round(score_performance,1) AS performance, round(ajust_niveau,1) AS aj_niveau,
    round(ajust_age,1) AS aj_age, round(progression_credible,1) AS progression,
    round(gros_matchs_percentile,0) AS gros_matchs, round(opp_coef_avg,3) AS coef_adv,
    rang_archetype AS rang_mondial, round(score_percentile,1) AS percentile,
    round(base,1) AS base, round(excellence,1) AS excellence, round(fragilite,1) AS fragilite,
    pilier_fort, pilier_faible, taille_cm, n_matches_oppw AS matchs,
    position AS poste, side, round(attdef_coef_att_avg,3) AS coef_att,
    round(attdef_coef_def_avg,3) AS coef_def, round(club_rating,3) AS rating_club,
    round(competition_avg_rating,3) AS rating_ligue,
    round(gros_matchs_delta,1) AS gros_matchs_delta, archetype AS archetype_courant,
    playerId, squadId, iterationId, position"""

# ----------------------------------------------------------------- vue club
club_param = st.query_params.get("club")
if club_param:
    squad_id, iter_id = (int(x) for x in club_param.split("-"))
    entete = requete("""SELECT club, competition, saison, pays, niveau,
            round(club_rating,3) AS rating, count(*) AS lignes
        FROM v_joueurs WHERE squadId=? AND iterationId=? GROUP BY ALL""", (squad_id, iter_id))
    if entete.empty:
        st.error("Club introuvable."); st.stop()
    e = entete.iloc[0]
    if st.button("← Retour à la recherche"):
        st.query_params.clear(); st.rerun()
    st.title(f"🏟️ {e.club}")
    st.caption(f"{e.competition} · {e.saison}"
               + (f" · {e.pays}" if pd.notna(e.pays) else "")
               + f" · rating club {n_(e.rating, '{:.3f}')}")

    effectif = requete(f"""SELECT {CHAMPS} FROM v_joueurs
        WHERE squadId=? AND iterationId=?
        QUALIFY row_number() OVER (PARTITION BY playerId ORDER BY score DESC) = 1
        ORDER BY position, score DESC""", (squad_id, iter_id))
    m1, m2, m3 = st.columns(3)
    m1.metric("Joueurs", len(effectif))
    m2.metric("Score médian", n_(effectif["score"].median()))
    m3.metric("Âge médian", n_(effectif["age"].median()))
    st.caption("👉 Clique sur une ligne pour ouvrir la fiche du joueur.")
    ev = st.dataframe(
        effectif, hide_index=True, width="stretch", height=430, key="effectif",
        on_select="rerun", selection_mode="single-row",
        column_order=["nom", "poste", "archetype_courant", "score", "age", "minutes",
                      "pied_fort", "performance", "progression", "gros_matchs"],
        column_config={"score": st.column_config.ProgressColumn(
            "Score", min_value=0, max_value=110, format="%.1f"),
            "archetype_courant": st.column_config.TextColumn("Archétype")})
    st.divider()
    sel = ev.selection["rows"] if ev and "rows" in ev.selection else []
    carte_joueur(effectif.iloc[sel[0] if sel else 0])

else:
    # ------------------------------------------------------------- vue joueurs
    st.title(f"{archetype} — {ARCHETYPES[archetype]}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Joueurs trouvés", f"{len(res)}")
    c2.metric("Score médian", f"{res['score'].median():.1f}" if len(res) else "—")
    c3.metric("Meilleur score", f"{res['score'].max():.1f}" if len(res) else "—")
    c4.metric("Âge médian", f"{res['age'].median():.1f}" if len(res) else "—")

    if res.empty:
        st.warning("Aucun joueur ne correspond à ces filtres.")
        st.stop()
    if len(res) == 500:
        st.caption("⚠️ Affichage limité aux 500 meilleurs scores — affine les filtres pour voir le reste.")

    res = res.copy()
    res["archetype_courant"] = archetype
    st.caption("👉 Clique sur une ligne pour ouvrir la fiche du joueur.")
    event = st.dataframe(
        res, hide_index=True, width="stretch", height=430, key="tableau",
        on_select="rerun", selection_mode="single-row",
        column_order=["nom", "club", "competition", "pays", "niveau", "saison",
                      "score", "age", "minutes", "pied_fort", "performance", "aj_niveau",
                      "aj_age", "progression", "gros_matchs", "coef_adv", "rang_mondial"],
        column_config={
            "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=110, format="%.1f"),
            "gros_matchs": st.column_config.NumberColumn("Gros matchs", help="Percentile du poste : >50 = meilleur que ses pairs contre le tiers supérieur"),
            "progression": st.column_config.NumberColumn("Progression", help="Delta crédible de performance sur les ~10 derniers matchs"),
            "coef_adv": st.column_config.NumberColumn("Coef adv.", help="Coefficient adversaire moyen : >1 = calendrier plus dur que son club"),
        })
    d1, d2, d3 = st.columns([2, 2, 1])
    d1.download_button("Télécharger la sélection (CSV)", res.to_csv(index=False).encode("utf-8"),
                       f"scouting_{archetype}.csv", "text/csv", width="stretch")
    clubs = res[["club", "squadId", "iterationId"]].drop_duplicates().sort_values("club")
    club_choisi = d2.selectbox("Voir l'effectif d'un club", ["—"] + clubs["club"].tolist(),
                               label_visibility="collapsed")
    if club_choisi != "—" and d3.button("🏟️ Ouvrir", width="stretch"):
        c_ = clubs[clubs["club"] == club_choisi].iloc[0]
        st.query_params["club"] = f"{int(c_.squadId)}-{int(c_.iterationId)}"
        st.rerun()
    st.divider()
    lignes = event.selection["rows"] if event and "rows" in event.selection else []
    carte_joueur(res.iloc[lignes[0] if lignes else 0])

# ----------------------------------------------------------------- mes listes
st.divider()
st.subheader("Mes listes")
o1, o2 = st.tabs([f"Shortlist « {shortlist} »", "Joueurs exclus"])
for onglet, nom_liste in ((o1, shortlist), (o2, "exclus")):
    with onglet:
        contenu_df = contenu(nom_liste)
        if contenu_df.empty:
            st.caption("Liste vide.")
            continue
        st.dataframe(contenu_df.drop(columns=["playerId"]), hide_index=True, width="stretch")
        a_retirer = st.selectbox("Retirer un joueur", ["—"] + contenu_df["nom"].tolist(),
                                 key=f"retrait_{nom_liste}")
        if a_retirer != "—" and st.button("Retirer", key=f"btn_retrait_{nom_liste}"):
            retirer(nom_liste, contenu_df.loc[contenu_df["nom"] == a_retirer, "playerId"].iloc[0])
            st.rerun()
        st.download_button("Exporter (CSV)", contenu_df.to_csv(index=False).encode("utf-8"),
                           f"{nom_liste}.csv", "text/csv", key=f"dl_{nom_liste}")
