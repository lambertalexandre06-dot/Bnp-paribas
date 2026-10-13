import os
import json
import time
from io import BytesIO
from flask import Flask, render_template, request, redirect, url_for, flash, abort, session, jsonify, Response, stream_with_context, g
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_wtf import CSRFProtect
from fpdf import FPDF
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import random
import string

app = Flask(__name__)
_default_secret = 'elitebank_super_secret_key_12345'
_secret_key = os.environ.get('SECRET_KEY', _default_secret)
if _secret_key == _default_secret:
    import warnings
    warnings.warn(
        "[SECURITE] SECRET_KEY utilise la valeur par defaut ! Definissez la variable d'environnement SECRET_KEY en production.",
        stacklevel=1
    )
app.config['SECRET_KEY'] = _secret_key
app.config['WTF_CSRF_TIME_LIMIT'] = 3600  # 1 heure

_basedir = os.path.abspath(os.path.dirname(__file__))
_instance = os.path.join(_basedir, 'instance')
os.makedirs(_instance, exist_ok=True)
_default_db = 'sqlite:///' + os.path.join(_instance, 'bnpparibas.db').replace('\\', '/')
database_url = os.environ.get('DATABASE_URL', _default_db)
# Render provides 'postgres://', but SQLAlchemy requires 'postgresql://'
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    num_client = db.Column(db.String(20), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='client') # 'admin' or 'client'
    prenom = db.Column(db.String(100), nullable=False)
    nom = db.Column(db.String(100), nullable=False)
    date_naissance = db.Column(db.String(20), nullable=False)
    adresse = db.Column(db.String(255), nullable=False)
    is_frozen = db.Column(db.Boolean, default=False)
    
    comptes = db.relationship('Compte', backref='client', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Compte(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    iban = db.Column(db.String(34), unique=True, nullable=False)
    solde = db.Column(db.Float, default=0.0)
    
    transactions_sortantes = db.relationship('Transaction', foreign_keys='Transaction.compte_source_id', backref='source', lazy=True)
    transactions_entrantes = db.relationship('Transaction', foreign_keys='Transaction.compte_dest_id', backref='destination', lazy=True)


class Tiers(db.Model):
    """Bénéficiaires persistants en base (remplace le stockage en session)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    nom = db.Column(db.String(150), nullable=False)
    iban = db.Column(db.String(34), nullable=False)
    valide = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('tiers', lazy=True))

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    compte_source_id = db.Column(db.Integer, db.ForeignKey('compte.id'), nullable=True) # None si dépôt initial
    compte_dest_id = db.Column(db.Integer, db.ForeignKey('compte.id'), nullable=False)
    montant = db.Column(db.Float, nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    type_trans = db.Column(db.String(50), nullable=False) # 'virement', 'depot'
    virement_mode = db.Column(db.String(30), nullable=True) # 'standard', 'instantane'
    libelle = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='valide')
    
    # Informations du bénéficiaire (pour virements)
    beneficiaire_nom = db.Column(db.String(70), nullable=True)
    beneficiaire_prenom = db.Column(db.String(70), nullable=True)
    beneficiaire_iban = db.Column(db.String(34), nullable=True)
    beneficiaire_bic = db.Column(db.String(11), nullable=True)
    beneficiaire_email = db.Column(db.String(100), nullable=True)
    beneficiaire_tel = db.Column(db.String(20), nullable=True)
    date_execution_prevue = db.Column(db.DateTime, nullable=True)
    transaction_ref = db.Column(db.String(32), unique=True, nullable=True, index=True)
    end_to_end_ref = db.Column(db.String(40), unique=False, nullable=True)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message = db.Column(db.String(500), nullable=False)
    type_notif = db.Column(db.String(20), default='info')
    read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('notifications', lazy=True))


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def _iban_checksum(country, bban):
    """Calcule les 2 chiffres de contrôle d'un IBAN (ISO 13616)."""
    rearranged = bban + country + '00'
    numeric = ''.join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    check = 98 - (int(numeric) % 97)
    return f"{check:02d}"


def validate_iban(iban):
    """Valide un IBAN par algorithme Mod-97 (ISO 13616). Retourne True si valide."""
    iban = (iban or '').upper().replace(' ', '').strip()
    if len(iban) < 15:
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = ''
    for c in rearranged:
        if c.isalpha():
            numeric += str(ord(c) - 55)
        elif c.isdigit():
            numeric += c
        else:
            return False
    return int(numeric) % 97 == 1


def generate_iban():
    """Génère un IBAN FR valide avec checksum correct."""
    # BBAN BNP Paribas : code banque 30004, code guichet 00001, n° compte 11 chiffres, clé RIB 2 chiffres
    account_number = ''.join(random.choices(string.digits, k=11))
    rib_key = ''.join(random.choices(string.digits, k=2))
    bban = f"30004000010{account_number}{rib_key}"
    check = _iban_checksum('FR', bban)
    return f"FR{check}{bban}"


def generate_num_client():
    return ''.join(random.choices(string.digits, k=10))


def generate_transaction_ref():
    # Format: OPE-YYYYMMDD-6CHAR
    date_part = datetime.utcnow().strftime('%Y%m%d')
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=7))
    return f"OPE-{date_part}-{suffix}"


def generate_end_to_end_ref():
    # SEPA end-to-end reference example: SEPAFRYYYYMMDDNNNNN
    date_part = datetime.utcnow().strftime('%Y%m%d')
    suffix = ''.join(random.choices(string.digits, k=7))
    return f"SEPAFR{date_part}{suffix}"

def recalculate_balance(compte_id):
    """Recalcule le solde d'un compte depuis les transactions en base.
    
    Règle :
    - CREDIT  : toutes transactions où compte_dest_id == compte.id ET status == 'valide'
    - DEBIT   : toutes transactions où compte_source_id == compte.id ET status == 'valide'
    
    Cela évite tout double-comptage (pas de séparation dépôt/virement côté crédit).
    """
    if not compte_id:
        return
    compte = Compte.query.get(compte_id)
    if not compte:
        return

    # Toutes les entrées validées (dépôts + virements reçus)
    inbound = db.session.query(db.func.sum(Transaction.montant)).filter(
        Transaction.compte_dest_id == compte.id,
        Transaction.status == 'valide'
    ).scalar() or 0.0

    # Toutes les sorties validées (retraits + virements envoyés)
    outbound = db.session.query(db.func.sum(Transaction.montant)).filter(
        Transaction.compte_source_id == compte.id,
        Transaction.status == 'valide'
    ).scalar() or 0.0

    compte.solde = inbound - outbound
    db.session.commit()


def format_eur(amount):
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        amount = 0.0
    negative = amount < 0
    amount = abs(amount)
    s = f"{amount:,.2f}"
    int_part, dec = s.split(".")
    int_part = int_part.replace(",", " ")
    formatted = f"{int_part},{dec} EUR"
    return f"- {formatted}" if negative else formatted


def format_eur_signed(amount):
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        amount = 0.0
    if amount < 0:
        return "- " + format_eur(abs(amount))
    return format_eur(amount)


@app.template_filter("eur_signed")
def eur_signed_filter(amount):
    return format_eur_signed(amount)


@app.template_filter("eur")
def eur_filter(amount):
    return format_eur(amount)


def transaction_status_label(tx):
    status = getattr(tx, 'status', 'valide')
    if status == 'bloque_incomplet':
        return "Bloqué : Dossier administratif incomplet"
    if status == 'bloque_reglementaire':
        if tx.type_trans == 'virement':
            return "Virement bloqué : En attente de validation réglementaire"
        return "En attente de validation réglementaire"
    if status == 'en_attente':
        return "En attente de validation"
    return "Validé"


@app.template_filter("status_label")
def status_label_filter(tx):
    return transaction_status_label(tx)


def client_initiales(user):
    p = (user.prenom or "P")[0].upper()
    n = (user.nom or "N")[0].upper()
    return f"{p}{n}"


def get_last_connection():
    return session.get("last_connection", datetime.now().strftime("%d/%m/%Y À %H:%M:%S"))


def normalize_iban(iban):
    return (iban or '').upper().replace(' ', '').strip()


def push_notification(user_id, message, type_notif='info'):
    if not user_id:
        return
    notif = Notification(user_id=user_id, message=message, type_notif=type_notif)
    db.session.add(notif)
    db.session.commit()


def count_unread_notifications(user_id):
    return Notification.query.filter_by(user_id=user_id, read=False).count()


@app.context_processor
def inject_notifications_count():
    if current_user.is_authenticated:
        return {'notif_unread': count_unread_notifications(current_user.id)}
    return {'notif_unread': 0}


def get_salutation():
    """Salutation selon l'heure locale (comme Elite Bank Web)."""
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "Bonjour"
    if 12 <= hour < 18:
        return "Bon après-midi"
    return "Bonsoir"


@app.context_processor
def inject_salutation():
    ctx = {"salutation": "", "salutation_complete": ""}
    if current_user.is_authenticated and getattr(current_user, "prenom", None):
        s = get_salutation()
        ctx["salutation"] = s
        ctx["salutation_complete"] = f"{s}, {current_user.prenom} {current_user.nom}"
    return ctx


def get_tiers_list():
    """Retourne les tiers depuis la base de données (persistants)."""
    if not current_user.is_authenticated:
        return []
    rows = Tiers.query.filter_by(user_id=current_user.id).order_by(Tiers.created_at.asc()).all()
    return [
        {"id": t.id, "nom": t.nom, "iban": t.iban, "valide": t.valide}
        for t in rows
    ]


def add_tiers(nom, iban):
    """Ajoute un tiers en base de données (en attente de validation)."""
    if not current_user.is_authenticated:
        return
    tiers = Tiers(
        user_id=current_user.id,
        nom=nom,
        iban=iban.upper().replace(' ', ''),
        valide=False,
    )
    db.session.add(tiers)
    db.session.commit()


def transaction_type_label(tx, compte_id):
    if tx.type_trans == "virement":
        if getattr(tx, 'virement_mode', None) == 'instantane':
            return "Virement instantané"
        return "Virement SEPA standard"
    if tx.type_trans == "retrait":
        return "Prélèvement"
    if "taxe" in (tx.libelle or "").lower():
        return "Paiement de taxes"
    if tx.type_trans == "depot":
        return "Crédit"
    return tx.type_trans.capitalize()


def serialize_operation(tx, compte_id):
    if tx.type_trans == "virement":
        is_credit = tx.compte_dest_id == compte_id and tx.compte_source_id != compte_id
    elif tx.type_trans == "retrait":
        is_credit = False
    else:
        is_credit = tx.compte_dest_id == compte_id
    return {
        "id": tx.id,
        "date_str": tx.date.strftime("%d/%m/%Y") if tx.date else "",
        "type_label": transaction_type_label(tx, compte_id),
        "libelle": tx.libelle,
        "montant": tx.montant,
        "positive": is_credit,
        "status": getattr(tx, 'status', 'valide'),
        "status_label": transaction_status_label(tx),
        "receipt_url": url_for('virement_recu', tx_id=tx.id) if tx.type_trans == 'virement' else None,
    }


def get_real_comptes(user, compte):
    """Comptes réels en base — même logique qu'Elite Bank Web."""
    if not compte:
        return [], []
    courant = {
        "id": compte.id,
        "libelle": f"Compte Courant — {user.prenom} {user.nom}",
        "iban": compte.iban,
        "solde": compte.solde,
        "type": "courant",
    }
    return [courant], []


def build_dashboard_context(user, compte):
    comptes_courants, comptes_epargne = get_real_comptes(user, compte)
    all_comptes = comptes_courants + comptes_epargne
    total_solde = compte.solde if compte else 0.0

    transactions = []
    if compte:
        transactions = Transaction.query.filter(
            (Transaction.compte_source_id == compte.id)
            | (Transaction.compte_dest_id == compte.id)
        ).order_by(Transaction.date.desc()).limit(100).all()

    operations = [serialize_operation(tx, compte.id) for tx in transactions] if compte else []
    operations_avenir = operations[:12]

    carte, virements, taxes = 0.0, 0.0, 0.0
    for tx in transactions:
        m = float(tx.montant)
        if tx.type_trans == "virement":
            virements += m
        elif tx.type_trans == "retrait" or "taxe" in (tx.libelle or "").lower():
            taxes += m
        else:
            carte += m
    if carte + virements + taxes == 0:
        carte, virements, taxes = 35.0, 45.0, 20.0

    base = max(total_solde, compte.solde if compte else 1000)
    sparkline = [
        round(base * 0.92, 2),
        round(base * 0.95, 2),
        round(base * 0.93, 2),
        round(base * 0.97, 2),
        round(base * 0.96, 2),
        round(base * 1.0, 2),
        round(base, 2),
    ]

    return {
        "compte": compte,
        "initiales": client_initiales(user),
        "last_connection": get_last_connection(),
        "salutation": get_salutation(),
        "salutation_complete": f"{get_salutation()}, {user.prenom}",
        "nb_comptes": len(all_comptes),
        "total_solde": total_solde,
        "comptes_courants": comptes_courants,
        "comptes_epargne": comptes_epargne,
        "all_comptes": all_comptes,
        "operations_avenir": operations_avenir,
        "operations": operations,
        "sparkline_data": sparkline,
        "chart_repartition": {
            "labels": ["Opérations carte", "Virements", "Taxes"],
            "amounts": [carte, virements, taxes],
        },
        "transactions": transactions[:10],
    }


def require_client():
    if current_user.role != "client":
        return redirect(url_for("admin_dashboard"))
    return None


# Pages d'aide (accessibles sans connexion)
@app.route('/mot-de-passe-oublie', methods=['GET', 'POST'])
def mot_de_passe_oublie():
    if request.method == 'POST':
        flash('Si votre numéro client est reconnu, un conseiller vous contactera sous 24h.', 'success')
        return redirect(url_for('login'))
    return render_template(
        'accueil_page.html',
        page_title='Numéro client ou code secret oublié',
        page_body=(
            '<p>Saisissez votre numéro client. Un conseiller BNP Paribas vous recontactera.</p>'
            '<form method="POST" class="accueil-form" style="max-width:400px;margin-top:20px;">'
            '<label class="accueil-label" for="num">Numéro client</label>'
            '<input class="accueil-input" type="text" id="num" name="num_client" required>'
            '<button type="submit" class="accueil-btn-continuer" style="margin-top:16px;">Envoyer la demande</button>'
            '</form>'
        ),
    )


@app.route('/conseils-securite')
def conseils_securite():
    return render_template(
        'accueil_page.html',
        page_title='Conseils sécurité et fraude',
        page_body=(
            '<ul style="line-height:1.8;padding-left:20px;">'
            '<li>Vérifiez l\'URL avant chaque connexion.</li>'
            '<li>Ne communiquez jamais votre code secret par téléphone ou e-mail.</li>'
            '<li>Signalez toute opération suspecte à votre conseiller.</li>'
            '</ul>'
        ),
    )


def get_virement_bancaire_bnp_paribas_skill():
    return (
        '<h2>Virements BNP Paribas — Standard vs Instantané</h2>'
        '<h3>1. Les deux types de virement</h3>'
        '<h4>SEPA Standard</h4>'
        '<ul style="line-height:1.8;padding-left:20px;">'
        '<li><strong>Délai :</strong> <strong>J+1 jour ouvré</strong> (hors week-end et jours fériés)</li>'
        '<li><strong>Coût :</strong> <strong>Gratuit</strong></li>'
        '<li><strong>Annulation :</strong> <strong>Possible</strong> avant traitement (avant fin de journée J)</li>'
        '<li><strong>Plafond :</strong> 6 000 €/jour (20 000 €/jour avec Clé Digitale)</li>'
        '<li><strong>Plafond virement permanent :</strong> 3 100 € max par virement</li>'
        '</ul>'
        '<h4>SEPA Instantané</h4>'
        '<table class="info-table" style="width:100%;border-collapse:collapse;margin-bottom:20px;">'
        '<thead><tr>'
        '<th style="border:1px solid #ddd;padding:8px;text-align:left;">Critère</th>'
        '<th style="border:1px solid #ddd;padding:8px;text-align:left;">SEPA Standard</th>'
        '<th style="border:1px solid #ddd;padding:8px;text-align:left;">SEPA Instantané</th>'
        '</tr></thead>'
        '<tbody>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Coût</td><td style="border:1px solid #ddd;padding:8px;">Gratuit</td><td style="border:1px solid #ddd;padding:8px;">Gratuit</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Délai</td><td style="border:1px solid #ddd;padding:8px;">J+1 ouvré</td><td style="border:1px solid #ddd;padding:8px;">&lt; 10 secondes</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Week-end / férié</td><td style="border:1px solid #ddd;padding:8px;">Traité le prochain jour ouvré</td><td style="border:1px solid #ddd;padding:8px;">Immédiat</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Plafond/jour</td><td style="border:1px solid #ddd;padding:8px;">6 000 € (ou 20 000 € avec Clé Digitale)</td><td style="border:1px solid #ddd;padding:8px;">5 000 €</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Annulation</td><td style="border:1px solid #ddd;padding:8px;">✅ Oui, avant traitement</td><td style="border:1px solid #ddd;padding:8px;">❌ Non</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Zone</td><td style="border:1px solid #ddd;padding:8px;">SEPA (36 pays)</td><td style="border:1px solid #ddd;padding:8px;">SEPA (si banque dest. compatible)</td></tr>'
        '</tbody>'
        '</table>'
        '<h3>3. Ce que voit le destinataire</h3>'
        '<table class="info-table" style="width:100%;border-collapse:collapse;margin-bottom:20px;">'
        '<thead><tr>'
        '<th style="border:1px solid #ddd;padding:8px;text-align:left;">Élément</th>'
        '<th style="border:1px solid #ddd;padding:8px;text-align:left;">SEPA Standard</th>'
        '<th style="border:1px solid #ddd;padding:8px;text-align:left;">SEPA Instantané</th>'
        '</tr></thead>'
        '<tbody>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Apparition sur le compte</td><td style="border:1px solid #ddd;padding:8px;">Fin de journée J+1 ouvré</td><td style="border:1px solid #ddd;padding:8px;">En moins de 10 secondes</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Libellé affiché</td><td style="border:1px solid #ddd;padding:8px;">Votre libellé + votre nom</td><td style="border:1px solid #ddd;padding:8px;">Identique, parfois mention &quot;INST&quot;</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Statut à réception</td><td style="border:1px solid #ddd;padding:8px;">Peut apparaître &quot;en attente&quot;</td><td style="border:1px solid #ddd;padding:8px;">Crédit immédiat et définitif</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Référence de transaction</td><td style="border:1px solid #ddd;padding:8px;">TxID visible dans les détails</td><td style="border:1px solid #ddd;padding:8px;">Identique</td></tr>'
        '</tbody>'
        '</table>'
        '<p>Le libellé est purement informatif — seul l\'IBAN détermine le routage du virement.</p>'
        '<h3>4. Sécurité et gestion des erreurs</h3>'
        '<h4>Erreur de montant</h4>'
        '<ul style="line-height:1.8;padding-left:20px;">'
        '<li><strong>Standard</strong> : annuler avant fin de journée et recréer le virement</li>'
        '<li><strong>Instantané</strong> : impossible d\'annuler — demander un remboursement amiable au destinataire</li>'
        '</ul>'
        '<h4>Erreur d\'IBAN</h4>'
        '<ul style="line-height:1.8;padding-left:20px;">'
        '<li><strong>Standard</strong> : récupérable via procédure bancaire (R-transaction SEPA), quelques jours</li>'
        '<li><strong>Instantané</strong> : récupération très difficile — BNP peut initier un &quot;recall&quot; sans garantie de succès</li>'
        '</ul>'
        '<h4>Erreur de libellé</h4>'
        '<ul style="line-height:1.8;padding-left:20px;">'
        '<li>Sans impact sur l\'exécution dans les deux cas</li>'
        '<li>Le virement est routé uniquement sur la base de l\'IBAN</li>'
        '</ul>'
        '<h4>Règles de prudence</h4>'
        '<ul style="line-height:1.8;padding-left:20px;">'
        '<li>✅ Vérifier l\'IBAN <strong>caractère par caractère</strong> avant tout virement instantané</li>'
        '<li>✅ Pour un premier envoi vers un IBAN inconnu → préférer le <strong>standard</strong> (annulable)</li>'
        '<li>❌ Ne jamais effectuer un instantané suite à une demande urgente non vérifiée (arnaque au faux virement)</li>'
        '</ul>'
        '<h3>5. Preuve de virement</h3>'
        '<p>Le justificatif PDF généré après chaque virement contient :</p>'
        '<ul style="line-height:1.8;padding-left:20px;">'
        '<li>Date et heure d\'exécution</li>'
        '<li>Montant</li>'
        '<li>IBAN émetteur et destinataire</li>'
        '<li>Libellé saisi</li>'
        '<li>Référence unique de transaction (TxID / EndToEndId)</li>'
        '</ul>'
        '<h4>Partager la preuve au destinataire</h4>'
        '<table class="info-table" style="width:100%;border-collapse:collapse;margin-bottom:20px;">'
        '<thead><tr>'
        '<th style="border:1px solid #ddd;padding:8px;text-align:left;">Méthode</th>'
        '<th style="border:1px solid #ddd;padding:8px;text-align:left;">Usage</th>'
        '</tr></thead>'
        '<tbody>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Reçu PDF</td><td style="border:1px solid #ddd;padding:8px;">Téléchargeable depuis l\'historique des opérations → partager par email ou messagerie</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Référence TxID</td><td style="border:1px solid #ddd;padding:8px;">À communiquer pour traçabilité en agence</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Capture d\'écran</td><td style="border:1px solid #ddd;padding:8px;">Acceptable informellement, non officielle</td></tr>'
        '</tbody>'
        '</table>'
        '<h3>6. Tableau de décision</h3>'
        '<table class="info-table" style="width:100%;border-collapse:collapse;margin-bottom:20px;">'
        '<thead><tr>'
        '<th style="border:1px solid #ddd;padding:8px;text-align:left;">Situation</th>'
        '<th style="border:1px solid #ddd;padding:8px;text-align:left;">Recommandation</th>'
        '</tr></thead>'
        '<tbody>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Urgence, besoin immédiat</td><td style="border:1px solid #ddd;padding:8px;">⚡ Instantané</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Premier virement vers un IBAN inconnu</td><td style="border:1px solid #ddd;padding:8px;">🕐 Standard</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Week-end ou jour férié</td><td style="border:1px solid #ddd;">⚡ Instantané</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Montant > 5 000 €</td><td style="border:1px solid #ddd;padding:8px;">🕐 Standard</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Doute sur le bénéficiaire</td><td style="border:1px solid #ddd;padding:8px;">🕐 Standard</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Preuve immédiate requise</td><td style="border:1px solid #ddd;padding:8px;">⚡ Instantané</td></tr>'
        '<tr><td style="border:1px solid #ddd;padding:8px;">Virement récurrent / programmé</td><td style="border:1px solid #ddd;padding:8px;">🕐 Virement permanent standard</td></tr>'
        '</tbody>'
        '</table>'
    )


@app.route('/virements-bnp')
def virements_bnp():
    return render_template(
        'accueil_page.html',
        page_title='Virements BNP Paribas',
        page_body=get_virement_bancaire_bnp_paribas_skill(),
    )


@app.route('/assistance-technique', methods=['GET', 'POST'])
def assistance_technique():
    if request.method == 'POST':
        flash('Votre demande a été enregistrée. Support : 09 69 39 55 55.', 'success')
        return redirect(url_for('login'))
    return render_template(
        'accueil_page.html',
        page_title='Assistance technique',
        page_body=(
            '<p><strong>09 69 39 55 55</strong> — Du lundi au vendredi, 8h-20h.</p>'
            '<form method="POST" class="accueil-form" style="max-width:400px;margin-top:20px;">'
            '<label class="accueil-label">Votre message</label>'
            '<textarea name="message" rows="4" class="accueil-input" required style="resize:vertical;"></textarea>'
            '<button type="submit" class="accueil-btn-continuer" style="margin-top:16px;">Envoyer</button>'
            '</form>'
        ),
    )


# Routes
@app.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.role == 'admin':
            return redirect(url_for('admin_dashboard'))
        else:
            return redirect(url_for('client_dashboard'))
    return render_template('login.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
        
    if request.method == 'POST':
        num_client = (request.form.get('num_client') or '').strip()
        password = request.form.get('password') or ''

        if not num_client:
            flash('Veuillez saisir votre numéro client.', 'error')
            return render_template('login.html')

        if not password:
            flash('Veuillez saisir votre code provisoire remis par votre conseiller.', 'error')
            return render_template('login.html', num_client=num_client, show_step2=True)

        user = User.query.filter_by(num_client=num_client).first()

        if user and user.check_password(password):
            login_user(user)
            session["last_connection"] = datetime.now().strftime("%d/%m/%Y À %H:%M:%S")
            push_notification(
                user.id,
                f"Connexion réussie — {get_salutation()} {user.prenom}.",
                'success',
            )
            return redirect(url_for('index'))

        flash('Numéro client ou code provisoire incorrect.', 'error')
        return render_template('login.html', num_client=num_client, show_step2=True)

    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for('login'))

# --- ADMIN ROUTES ---
@app.route('/admin')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        abort(403)
    clients = User.query.filter_by(role='client').all()
    
    pending_transactions = Transaction.query.filter_by(status='en_attente').order_by(Transaction.date.desc()).all()
    pending_virements = []
    for tx in pending_transactions:
        source_compte = Compte.query.get(tx.compte_source_id) if tx.compte_source_id else None
        source_user = User.query.get(source_compte.user_id) if source_compte else None
        
        dest_compte = Compte.query.get(tx.compte_dest_id) if tx.compte_dest_id else None
        dest_user = User.query.get(dest_compte.user_id) if dest_compte else None
        
        pending_virements.append({
            'transaction': tx,
            'source_compte': source_compte,
            'source_user': source_user,
            'dest_compte': dest_compte,
            'dest_user': dest_user
        })
        
    return render_template('admin_dashboard.html', clients=clients, pending_virements=pending_virements)

@app.route('/admin/virement/valider/<int:tx_id>')
@login_required
def admin_valider_virement(tx_id):
    if current_user.role != 'admin':
        abort(403)

    tx = Transaction.query.get_or_404(tx_id)
    if tx.status != 'en_attente':
        flash('Cette transaction a déjà été traitée.', 'error')
        return redirect(url_for('admin_dashboard'))

    # Récupérer les comptes AVANT de changer le statut
    source_compte = Compte.query.get(tx.compte_source_id) if tx.compte_source_id else None
    dest_compte = Compte.query.get(tx.compte_dest_id) if tx.compte_dest_id else None

    # Marquer comme validé
    tx.status = 'valide'
    db.session.commit()

    # Recalculer les soldes via la fonction canonique (évite toute double déduction)
    if source_compte:
        recalculate_balance(source_compte.id)
    if dest_compte and (not source_compte or dest_compte.id != source_compte.id):
        recalculate_balance(dest_compte.id)

    # Notifications
    if source_compte:
        push_notification(
            source_compte.user_id,
            f"Votre virement de {tx.montant:.2f} EUR a été validé par votre conseiller.",
            'success',
        )
    if dest_compte and (not source_compte or dest_compte.id != source_compte.id):
        source_user = User.query.get(source_compte.user_id) if source_compte else None
        sender_name = f"{source_user.prenom} {source_user.nom}" if source_user else "un tiers"
        push_notification(
            dest_compte.user_id,
            f"Virement reçu : {tx.montant:.2f} EUR de {sender_name}.",
            'success',
        )

    flash('Le virement a été validé avec succès.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/virement/bloquer/<int:tx_id>')
@login_required
def admin_bloquer_virement(tx_id):
    if current_user.role != 'admin':
        abort(403)
        
    tx = Transaction.query.get_or_404(tx_id)
    if tx.status != 'en_attente':
        flash('Cette transaction a déjà été traitée.', 'error')
        return redirect(url_for('admin_dashboard'))
        
    tx.status = 'bloque_reglementaire'
    
    # Pas de restitution nécessaire : le solde n'a jamais été déduit (le virement était en attente)
    source_compte = Compte.query.get(tx.compte_source_id) if tx.compte_source_id else None
    if source_compte:
        push_notification(
            source_compte.user_id,
            f"Votre virement de {tx.montant:.2f} EUR vers {tx.libelle} a été refusé et bloqué par la banque.",
            'error'
        )
        
    db.session.commit()
    flash('Le virement a été bloqué et annulé.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/create_client', methods=['GET', 'POST'])
@login_required
def admin_create_client():
    if current_user.role != 'admin':
        abort(403)
        
    if request.method == 'POST':
        prenom = (request.form.get('prenom') or '').strip()
        nom = (request.form.get('nom') or '').strip()
        date_naissance = (request.form.get('date_naissance') or '').strip()
        adresse = (request.form.get('adresse') or '').strip()
        password = request.form.get('password') or ''
        try:
            solde_initial = float(request.form.get('solde') or 0)
        except ValueError:
            solde_initial = 0.0

        if not all([prenom, nom, date_naissance, adresse, password]):
            flash('Tous les champs sont obligatoires, y compris le code provisoire.', 'error')
            return render_template('create_client.html')

        try:
            num_client = generate_num_client()
            while User.query.filter_by(num_client=num_client).first():
                num_client = generate_num_client()

            new_user = User(
                num_client=num_client,
                prenom=prenom,
                nom=nom,
                date_naissance=date_naissance,
                adresse=adresse,
                role='client',
            )
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.flush()

            iban = generate_iban()
            while Compte.query.filter_by(iban=iban).first():
                iban = generate_iban()

            new_compte = Compte(user_id=new_user.id, iban=iban, solde=solde_initial)
            db.session.add(new_compte)
            db.session.flush()

            if solde_initial > 0:
                depot = Transaction(
                    compte_dest_id=new_compte.id,
                    montant=solde_initial,
                    type_trans='depot',
                    libelle='Dépôt initial',
                )
                db.session.add(depot)

            db.session.commit()

            push_notification(
                new_user.id,
                f"Bienvenue {new_user.prenom}. Votre espace client BNP Paribas est actif.",
                'success',
            )
            flash(
                f'Client enregistré en base. N° client : {num_client} — '
                f'Communiquez le code provisoire au client.',
                'success',
            )
            return redirect(url_for('admin_dashboard'))
        except Exception:
            db.session.rollback()
            flash('Erreur lors de l\'enregistrement du client. Réessayez.', 'error')
            return render_template('create_client.html')
        
    return render_template('create_client.html')

@app.route('/admin/toggle_freeze/<int:user_id>')
@login_required
def toggle_freeze(user_id):
    if current_user.role != 'admin':
        abort(403)
    user = User.query.get_or_404(user_id)
    if user.role != 'admin':
        user.is_frozen = not user.is_frozen
        db.session.commit()
        status = "gelé" if user.is_frozen else "dégelé"
        push_notification(
            user.id,
            f"Votre compte a été {status} par l'administration.",
            'warning' if user.is_frozen else 'info',
        )
        flash(f'Le compte de {user.prenom} {user.nom} a été {status}.', 'success')
    return redirect(url_for('admin_dashboard'))


# --- CLIENT ROUTES ---
@app.route('/admin/transaction/<int:user_id>', methods=['GET', 'POST'])
@login_required
def admin_transaction(user_id):
    if current_user.role != 'admin':
        abort(403)
    user = User.query.get_or_404(user_id)
    if user.role == 'admin':
        abort(400)
    
    compte = Compte.query.filter_by(user_id=user.id).first()
    
    if request.method == 'POST':
        montant = float(request.form.get('montant'))
        type_trans = request.form.get('type_trans') # 'depot' ou 'retrait' ou 'virement'
        libelle = request.form.get('libelle')
        date_str = request.form.get('date') # Format YYYY-MM-DD
        iban_dest = request.form.get('iban_dest')
        status = request.form.get('status', 'valide')
        
        try:
            trans_date = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            trans_date = datetime.utcnow()
            
        dest_id = compte.id
        if type_trans == 'virement':
            if not iban_dest:
                flash('Veuillez renseigner l\'IBAN du bénéficiaire pour un virement.', 'error')
                return redirect(url_for('admin_transaction', user_id=user.id))
            iban_dest = normalize_iban(iban_dest)
            libelle = f"{libelle} (Vers: {iban_dest})"

            compte_cible = Compte.query.filter_by(iban=iban_dest).first()
            if compte_cible:
                dest_id = compte_cible.id
            else:
                libelle = f"{libelle} (SEPA externe : {iban_dest})"

        transaction = Transaction(
            compte_dest_id=dest_id,
            compte_source_id=compte.id if type_trans in ('retrait', 'virement') else None,
            montant=montant,
            type_trans=type_trans,
            libelle=libelle,
            date=trans_date,
            status=status
        )
        db.session.add(transaction)
        db.session.commit()
        try:
            print(f"[TX] Created transaction id={transaction.id} ref={transaction.transaction_ref} e2e={transaction.end_to_end_ref}")
        except Exception:
            pass
        
        # Keep everything perfectly balanced in the database!
        recalculate_balance(compte.id)
        if type_trans == 'virement' and dest_id != compte.id:
            recalculate_balance(dest_id)
        
        push_notification(
            user.id,
            f"Nouveau mouvement : {libelle} — {montant:.2f} EUR.",
            'info',
        )
        flash(f'Transaction ajoutée avec succès pour {user.prenom} {user.nom}.', 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('admin_transaction.html', user=user, compte=compte)

@app.route('/admin/client/<int:user_id>')
@login_required
def admin_client_details(user_id):
    if current_user.role != 'admin':
        abort(403)
    user = User.query.get_or_404(user_id)
    compte = Compte.query.filter_by(user_id=user.id).first()
    
    transactions = []
    if compte:
        # Avoid duplicate transactions in the details view
        transactions = Transaction.query.filter(
            (Transaction.compte_source_id == compte.id) | (Transaction.compte_dest_id == compte.id)
        ).order_by(Transaction.date.desc()).all()
        
    return render_template('admin_client_details.html', user=user, compte=compte, transactions=transactions)

@app.route('/admin/client/delete/<int:user_id>')
@login_required
def admin_delete_client(user_id):
    if current_user.role != 'admin':
        abort(403)
    user = User.query.get_or_404(user_id)
    if user.role == 'admin':
        flash('Impossible de supprimer un compte administrateur.', 'error')
        return redirect(url_for('admin_dashboard'))
        
    comptes = Compte.query.filter_by(user_id=user.id).all()
    for compte in comptes:
        Transaction.query.filter(
            (Transaction.compte_source_id == compte.id) | (Transaction.compte_dest_id == compte.id)
        ).delete(synchronize_session=False)
        db.session.delete(compte)
    
    Notification.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    db.session.delete(user)
    db.session.commit()
    
    flash(f'Le compte client de {user.prenom} {user.nom} a été supprimé définitivement.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/transaction/edit/<int:trans_id>', methods=['GET', 'POST'])
@login_required
def admin_edit_transaction(trans_id):
    if current_user.role != 'admin':
        abort(403)
    transaction = Transaction.query.get_or_404(trans_id)
    compte = Compte.query.get(transaction.compte_dest_id)
    user = User.query.get(compte.user_id) if compte else None
    
    if request.method == 'POST':
        old_source_id = transaction.compte_source_id
        old_dest_id = transaction.compte_dest_id
        
        transaction.libelle = request.form.get('libelle')
        transaction.montant = float(request.form.get('montant'))
        transaction.type_trans = request.form.get('type_trans')
        transaction.status = request.form.get('status', 'valide')
        date_str = request.form.get('date')
        
        try:
            transaction.date = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            pass
            
        db.session.commit()
        
        # Keep everything perfectly balanced in the database!
        recalculate_balance(old_source_id)
        recalculate_balance(old_dest_id)
        
        flash('Transaction modifiée avec succès.', 'success')
        return redirect(url_for('admin_client_details', user_id=user.id) if user else url_for('admin_dashboard'))
        
    return render_template('admin_edit_transaction.html', transaction=transaction, user=user)

@app.route('/admin/transaction/delete/<int:trans_id>')
@login_required
def admin_delete_transaction(trans_id):
    if current_user.role != 'admin':
        abort(403)
    transaction = Transaction.query.get_or_404(trans_id)
    source_id = transaction.compte_source_id
    dest_id = transaction.compte_dest_id
    
    compte = Compte.query.get(dest_id)
    user = User.query.get(compte.user_id) if compte else None
    
    db.session.delete(transaction)
    db.session.commit()
    
    recalculate_balance(source_id)
    recalculate_balance(dest_id)
    
    flash('Transaction supprimée définitivement.', 'success')
    return redirect(url_for('admin_client_details', user_id=user.id) if user else url_for('admin_dashboard'))

@app.route('/dashboard')
@login_required
def client_dashboard():
    denied = require_client()
    if denied:
        return denied
    compte = Compte.query.filter_by(user_id=current_user.id).first()
    ctx = build_dashboard_context(current_user, compte)
    return render_template('client_dashboard.html', active_nav='accueil', **ctx)


@app.route('/dashboard/comptes')
@login_required
def dashboard_comptes():
    denied = require_client()
    if denied:
        return denied
    compte = Compte.query.filter_by(user_id=current_user.id).first()
    ctx = build_dashboard_context(current_user, compte)
    return render_template('dashboard_comptes.html', active_nav='comptes', **ctx)


@app.route('/dashboard/compte/<int:compte_id>')
@login_required
def dashboard_compte_detail(compte_id):
    denied = require_client()
    if denied:
        return denied
    compte = Compte.query.filter_by(user_id=current_user.id).first()
    ctx = build_dashboard_context(current_user, compte)
    if not compte or compte.id != compte_id:
        flash("Compte introuvable.", "error")
        return redirect(url_for("dashboard_comptes"))
    compte_info = {
        "id": compte.id,
        "libelle": f"Compte Courant — {current_user.prenom} {current_user.nom}",
        "iban": compte.iban,
        "solde": compte.solde,
    }
    mouvements = ctx["operations"][:15]
    return render_template(
        'dashboard_compte_detail.html',
        active_nav='comptes',
        compte_info=compte_info,
        mouvements=mouvements,
        **ctx,
    )


@app.route('/dashboard/operations')
@login_required
def dashboard_operations():
    denied = require_client()
    if denied:
        return denied
    compte = Compte.query.filter_by(user_id=current_user.id).first()
    ctx = build_dashboard_context(current_user, compte)
    return render_template('dashboard_operations.html', active_nav='suivi', **ctx)


@app.route('/dashboard/valider-virements')
@login_required
def dashboard_valider_virements():
    denied = require_client()
    if denied:
        return denied
    compte = Compte.query.filter_by(user_id=current_user.id).first()
    validated = session.get("validated_tx", [])
    virements_pending = []
    if compte:
        txs = Transaction.query.filter_by(
            compte_source_id=compte.id, type_trans="virement"
        ).order_by(Transaction.date.desc()).limit(10).all()
        for tx in txs:
            if tx.id not in validated:
                virements_pending.append(serialize_operation(tx, compte.id))
    ctx = build_dashboard_context(current_user, compte)
    return render_template(
        'dashboard_valider_virements.html',
        active_nav='suivi',
        virements_pending=virements_pending,
        **ctx,
    )


@app.route('/dashboard/valider-virement/<int:tx_id>', methods=['POST'])
@login_required
def dashboard_valider_virement(tx_id):
    denied = require_client()
    if denied:
        return denied
    validated = session.get("validated_tx", [])
    if tx_id not in validated:
        validated.append(tx_id)
    session["validated_tx"] = validated
    flash("Virement validé avec succès.", "success")
    return redirect(url_for("dashboard_valider_virements"))


@app.route('/dashboard/tiers/nouveau', methods=['GET', 'POST'])
@login_required
def dashboard_tiers_nouveau():
    denied = require_client()
    if denied:
        return denied
    if request.method == 'POST':
        nom = request.form.get("nom", "").strip()
        iban = normalize_iban(request.form.get("iban", ""))
        if not nom:
            flash("Veuillez renseigner le nom du bénéficiaire.", "error")
        elif not iban:
            flash("Veuillez renseigner l'IBAN du bénéficiaire.", "error")
        elif not validate_iban(iban):
            flash("L'IBAN saisi est invalide (vérification Mod-97 échouée).", "error")
        else:
            add_tiers(nom, iban)
            flash("Tiers enregistré. En attente de validation.", "success")
            return redirect(url_for("dashboard_tiers_valider"))
    compte = Compte.query.filter_by(user_id=current_user.id).first()
    ctx = build_dashboard_context(current_user, compte)
    return render_template('dashboard_tiers_nouveau.html', active_nav='saisie', **ctx)


@app.route('/dashboard/tiers/valider')
@login_required
def dashboard_tiers_valider():
    denied = require_client()
    if denied:
        return denied
    tiers_pending = [t for t in get_tiers_list() if not t.get("valide")]
    compte = Compte.query.filter_by(user_id=current_user.id).first()
    ctx = build_dashboard_context(current_user, compte)
    return render_template(
        'dashboard_tiers_valider.html',
        active_nav='suivi',
        tiers_pending=tiers_pending,
        **ctx,
    )


@app.route('/dashboard/tiers/valider/<int:tiers_id>', methods=['POST'])
@login_required
def dashboard_valider_tiers(tiers_id):
    denied = require_client()
    if denied:
        return denied
    # Mise à jour en base (persistant)
    tiers = Tiers.query.filter_by(id=tiers_id, user_id=current_user.id).first()
    if tiers:
        tiers.valide = True
        db.session.commit()
        flash("Tiers validé. Vous pouvez l'utiliser pour vos virements.", "success")
    else:
        flash("Tiers introuvable.", "error")
    return redirect(url_for("dashboard_tiers_valider"))


@app.route('/dashboard/recherche')
@login_required
def dashboard_search():
    denied = require_client()
    if denied:
        return denied
    q = request.args.get("q", "").strip()
    compte = Compte.query.filter_by(user_id=current_user.id).first()
    ctx = build_dashboard_context(current_user, compte)
    if q and compte:
        txs = Transaction.query.filter(
            ((Transaction.compte_source_id == compte.id) | (Transaction.compte_dest_id == compte.id)),
            Transaction.libelle.ilike(f"%{q}%"),
        ).order_by(Transaction.date.desc()).limit(100).all()
        ctx["operations"] = [serialize_operation(tx, compte.id) for tx in txs]
    else:
        flash("Saisissez un mot-clé pour rechercher.", "error")
    return render_template('dashboard_operations.html', active_nav='suivi', **ctx)


def _dashboard_simple(page_title, description, active_nav, template='dashboard_page.html'):
    compte = Compte.query.filter_by(user_id=current_user.id).first()
    ctx = build_dashboard_context(current_user, compte)
    return render_template(
        template,
        active_nav=active_nav,
        page_title=page_title,
        page_description=description,
        **ctx,
    )


@app.route('/dashboard/suivi')
@login_required
def dashboard_suivi():
    denied = require_client()
    if denied:
        return denied
    return redirect(url_for('dashboard_valider_virements'))


@app.route('/dashboard/placements')
@login_required
def dashboard_placements():
    denied = require_client()
    if denied:
        return denied
    return _dashboard_simple(
        "Placements",
        "Consultez vos placements et produits d'investissement entreprise.",
        "placements",
    )


@app.route('/dashboard/services')
@login_required
def dashboard_services():
    denied = require_client()
    if denied:
        return denied
    return _dashboard_simple(
        "Services",
        "Accédez aux services bancaires : affacturage, trade finance, cartes professionnelles.",
        "services",
    )


@app.route('/dashboard/outils')
@login_required
def dashboard_outils():
    denied = require_client()
    if denied:
        return denied
    return _dashboard_simple(
        "Outils",
        "Exportez vos relevés, configurez vos alertes et gérez vos préférences.",
        "outils",
    )


@app.route('/dashboard/informations-financieres')
@login_required
def dashboard_infos():
    denied = require_client()
    if denied:
        return denied
    return _dashboard_simple(
        "Informations financières",
        "Indicateurs, ratios et synthèse de votre activité.",
        "infos",
    )


@app.route('/dashboard/e-documents')
@login_required
def dashboard_edocuments():
    denied = require_client()
    if denied:
        return denied
    return _dashboard_simple(
        "E-Documents",
        "Retrouvez vos documents signés, RIB et attestations.",
        "edocuments",
    )


@app.route('/dashboard/utilisateurs')
@login_required
def dashboard_utilisateurs():
    denied = require_client()
    if denied:
        return denied
    return _dashboard_simple(
        "Gestion des utilisateurs",
        "Gérez les habilitations et accès de vos collaborateurs.",
        "utilisateurs",
    )


@app.route('/dashboard/historique')
@login_required
def dashboard_historique():
    denied = require_client()
    if denied:
        return denied
    return redirect(url_for('dashboard_operations'))


@app.route('/dashboard/parametres')
@login_required
def dashboard_parametres():
    denied = require_client()
    if denied:
        return denied
    return _dashboard_simple(
        "Paramètres",
        f"Compte : {current_user.num_client} — {current_user.prenom} {current_user.nom}",
        "accueil",
    )


@app.route('/dashboard/brouillons')
@login_required
def dashboard_brouillons():
    denied = require_client()
    if denied:
        return denied
    return _dashboard_simple(
        "Brouillons",
        "Aucun ordre en brouillon pour le moment.",
        "saisie",
    )


@app.route('/dashboard/alertes')
@login_required
def dashboard_alertes():
    denied = require_client()
    if denied:
        return denied
    Notification.query.filter_by(user_id=current_user.id, read=False).update({'read': True})
    db.session.commit()
    if current_user.is_frozen:
        push_notification(current_user.id, 'Rappel : vos opérations sont gelées.', 'warning')
    compte = Compte.query.filter_by(user_id=current_user.id).first()
    ctx = build_dashboard_context(current_user, compte)
    notifs = Notification.query.filter_by(user_id=current_user.id).order_by(
        Notification.created_at.desc()
    ).limit(50).all()
    return render_template(
        'dashboard_notifications.html',
        active_nav='accueil',
        notifications=notifs,
        **ctx,
    )


@app.route('/api/notifications')
@login_required
def api_notifications():
    notifs = Notification.query.filter_by(user_id=current_user.id).order_by(
        Notification.created_at.desc()
    ).limit(30).all()
    return jsonify([
        {
            'id': n.id,
            'message': n.message,
            'type': n.type_notif,
            'read': n.read,
            'date': n.created_at.strftime('%d/%m/%Y %H:%M') if n.created_at else '',
        }
        for n in notifs
    ])


@app.route('/api/notifications/stream')
@login_required
def api_notifications_stream():
    """SSE sécurisé : la boucle se termine après 60 itérations (~2 min) pour
    ne pas bloquer les workers Gunicorn. Le JS client se reconnecte automatiquement."""
    user_id = current_user.id

    def event_stream():
        last_id = 0
        with app.app_context():
            latest = Notification.query.filter_by(user_id=user_id).order_by(
                Notification.id.desc()
            ).first()
            if latest:
                last_id = latest.id
        yield ': ok\n\n'
        max_iterations = 60  # 60 × 2s = 2 min, puis reconnexion cliente
        for _ in range(max_iterations):
            try:
                with app.app_context():
                    new_items = Notification.query.filter(
                        Notification.user_id == user_id,
                        Notification.id > last_id,
                    ).order_by(Notification.id.asc()).all()
                    for n in new_items:
                        last_id = n.id
                        payload = json.dumps({
                            'id': n.id,
                            'message': n.message,
                            'type': n.type_notif,
                            'read': n.read,
                            'date': n.created_at.strftime('%d/%m/%Y %H:%M') if n.created_at else '',
                        })
                        yield f'data: {payload}\n\n'
            except Exception:
                pass
            time.sleep(2)

    return Response(
        stream_with_context(event_stream()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/notifications/read-all', methods=['POST'])
@login_required
@csrf.exempt  # Appelé par JavaScript (fetch), pas par un formulaire HTML
def api_notifications_read_all():
    Notification.query.filter_by(user_id=current_user.id, read=False).update({'read': True})
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/notifications/delete/<int:notif_id>', methods=['POST'])
@login_required
@csrf.exempt  # Appelé par JavaScript (fetch), pas par un formulaire HTML
def api_notification_delete(notif_id):
    notif = Notification.query.get_or_404(notif_id)
    if notif.user_id != current_user.id:
        abort(403)
    db.session.delete(notif)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/notifications/delete-all', methods=['POST'])
@login_required
@csrf.exempt  # Appelé par JavaScript (fetch), pas par un formulaire HTML
def api_notifications_delete_all():
    Notification.query.filter_by(user_id=current_user.id).delete(synchronize_session=False)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/virement', methods=['GET', 'POST'])
@login_required
def virement():
    if current_user.role != 'client':
        return redirect(url_for('admin_dashboard'))

    compte_source = Compte.query.filter_by(user_id=current_user.id).first()
    if not compte_source:
        flash('Aucun compte bancaire associé. Contactez votre administrateur.', 'error')
        return redirect(url_for('client_dashboard'))

    ctx = build_dashboard_context(current_user, compte_source)
    tiers_list = [t for t in get_tiers_list() if t.get('valide')]

    if request.method == 'POST':
        if current_user.is_frozen:
            flash('Opération impossible : vos mouvements sont gelés.', 'error')
            return redirect(url_for('virement'))

        # Récupération des données
        beneficiaire_nom = (request.form.get('beneficiaire_nom') or '').strip()
        beneficiaire_prenom = (request.form.get('beneficiaire_prenom') or '').strip()
        iban_dest = normalize_iban(request.form.get('iban_dest'))
        bic = (request.form.get('bic') or '').strip().upper()
        libelle = (request.form.get('libelle') or '').strip()
        montant_raw = request.form.get('montant')
        date_execution = request.form.get('date_execution')
        virement_mode = request.form.get('virement_mode') or 'standard'
        beneficiaire_email = (request.form.get('beneficiaire_email') or '').strip()
        beneficiaire_tel = (request.form.get('beneficiaire_tel') or '').strip()

        errors = []

        # Validations obligatoires
        if not beneficiaire_nom:
            errors.append('Le nom du bénéficiaire est obligatoire.')
        if not iban_dest or len(iban_dest) < 15:
            errors.append('IBAN du bénéficiaire obligatoire (format FR76...).')
        elif not validate_iban(iban_dest):
            errors.append('IBAN invalide : la vérification Mod-97 a échoué. Veuillez vérifier l\'IBAN saisi.')
        if not libelle:
            errors.append('Le motif du virement est obligatoire.')

        try:
            montant = float(montant_raw.replace(',', '.')) if montant_raw else 0
        except ValueError:
            montant = 0
        if montant <= 0:
            errors.append('Le montant doit être supérieur à 0.')

        if virement_mode not in ('standard', 'instantane'):
            virement_mode = 'standard'

        if virement_mode == 'instantane' and montant > 5000:
            errors.append('Le plafond pour un virement instantané est de 5 000 €.')

        # Validation du BIC (optionnel mais validé s'il est rempli)
        if bic and (len(bic) < 8 or len(bic) > 11):
            errors.append('Le BIC doit contenir entre 8 et 11 caractères.')

        # Validation de l'email (optionnel mais validé s'il est rempli)
        if beneficiaire_email and '@' not in beneficiaire_email:
            errors.append('L\'adresse email du bénéficiaire est invalide.')

        if errors:
            for err in errors:
                flash(err, 'error')
            return render_template(
                'virement.html',
                active_nav='saisie',
                tiers_list=tiers_list,
                form_data=request.form,
                **ctx,
            )

        if compte_source.solde < montant:
            flash('Solde insuffisant pour effectuer ce virement.', 'error')
            return render_template(
                'virement.html',
                active_nav='saisie',
                tiers_list=tiers_list,
                form_data=request.form,
                **ctx,
            )

        compte_dest = Compte.query.filter_by(iban=iban_dest).first()

        if compte_dest and compte_dest.id == compte_source.id:
            flash('Vous ne pouvez pas virer vers le même compte.', 'error')
            return render_template(
                'virement.html',
                active_nav='saisie',
                tiers_list=tiers_list,
                form_data=request.form,
                **ctx,
            )



        if compte_dest:
            # Virement interne : vers un autre compte connu dans notre base
            dest_id = compte_dest.id
            libelle_final = libelle
        else:
            # Virement SEPA externe : on utilise le compte source comme "proxy"
            # pour stocker la transaction (montant sera déduit à la validation).
            # Le compte_dest_id reste NULL-like via le compte source, le libellé
            # encode la destination réelle.
            # CORRECTION : on pointe vers le compte source pour ne PAS créditer
            # un compte inexistant, mais on le marque explicitement comme externe.
            dest_id = compte_source.id
            libelle_final = f"{libelle} (SEPA externe : {iban_dest})"

        # Parsing de la date d'exécution
        date_execution_prevue = None
        if date_execution:
            try:
                date_execution_prevue = datetime.strptime(date_execution, '%Y-%m-%d')
            except ValueError:
                pass

        # Le virement reste en attente de validation par l'admin
        # Le solde ne sera déduit que lorsque l'admin validera
        status = 'en_attente'

        # Generate unique references
        tx_ref = generate_transaction_ref()
        attempts = 0
        while Transaction.query.filter_by(transaction_ref=tx_ref).first() and attempts < 6:
            tx_ref = generate_transaction_ref()
            attempts += 1

        e2e_ref = generate_end_to_end_ref()
        e2e_attempts = 0
        while Transaction.query.filter_by(end_to_end_ref=e2e_ref).first() and e2e_attempts < 6:
            e2e_ref = generate_end_to_end_ref()
            e2e_attempts += 1

        transaction = Transaction(
            compte_source_id=compte_source.id,
            compte_dest_id=dest_id,
            montant=montant,
            type_trans='virement',
            virement_mode=virement_mode,
            libelle=libelle_final,
            status=status,
            beneficiaire_nom=beneficiaire_nom,
            beneficiaire_prenom=beneficiaire_prenom,
            beneficiaire_iban=iban_dest,
            beneficiaire_bic=bic if bic else None,
            beneficiaire_email=beneficiaire_email if beneficiaire_email else None,
            beneficiaire_tel=beneficiaire_tel if beneficiaire_tel else None,
            date_execution_prevue=date_execution_prevue,
            transaction_ref=tx_ref,
            end_to_end_ref=e2e_ref
        )
        db.session.add(transaction)
        db.session.commit()

        return redirect(url_for('client_dashboard'))

    return render_template(
        'virement.html',
        active_nav='saisie',
        tiers_list=tiers_list,
        form_data=None,
        **ctx,
    )


@app.route('/virement/recu/<int:tx_id>')
@login_required
def virement_recu(tx_id):
    tx = Transaction.query.get_or_404(tx_id)
    if current_user.role == 'client':
        compte = Compte.query.filter_by(user_id=current_user.id).first()
        if not compte or (tx.compte_source_id != compte.id and tx.compte_dest_id != compte.id):
            abort(403)
    source_compte = Compte.query.get(tx.compte_source_id) if tx.compte_source_id else None
    dest_compte = Compte.query.get(tx.compte_dest_id) if tx.compte_dest_id else None
    return render_template(
        'virement_recu.html',
        transaction=tx,
        source_compte=source_compte,
        dest_compte=dest_compte,
    )


@app.route('/virement/recu/<int:tx_id>/pdf')
@login_required
def virement_recu_pdf(tx_id):
    tx = Transaction.query.get_or_404(tx_id)
    if current_user.role == 'client':
        compte = Compte.query.filter_by(user_id=current_user.id).first()
        if not compte or (tx.compte_source_id != compte.id and tx.compte_dest_id != compte.id):
            abort(403)
    
    source_compte = Compte.query.get(tx.compte_source_id) if tx.compte_source_id else None
    dest_compte = Compte.query.get(tx.compte_dest_id) if tx.compte_dest_id else None
    source_user = User.query.get(source_compte.user_id) if source_compte else None

    # Ensure transaction references exist (for older records)
    updated = False
    try:
        if not getattr(tx, 'transaction_ref', None):
            tx.transaction_ref = generate_transaction_ref()
            updated = True
        if not getattr(tx, 'end_to_end_ref', None):
            tx.end_to_end_ref = generate_end_to_end_ref()
            updated = True
        if updated:
            db.session.commit()
            try:
                print(f"[PDF] Backfilled refs for tx id={tx.id} ref={tx.transaction_ref} e2e={tx.end_to_end_ref}")
            except Exception:
                pass
    except Exception as e:
        print(f"[PDF] Error ensuring refs for tx {tx_id}: {type(e).__name__}: {e}")

    pdf = FPDF(format='A4')
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font('Helvetica', '', 10)

    # Logo : convert and insert a JPEG copy to avoid PNG/alpha issues
    logo_src = os.path.join(_basedir, 'static', 'logo.png')
    temp_logo_jpg = os.path.join(_instance, 'logo_for_pdf.jpg')
    try:
        if os.path.exists(logo_src):
            try:
                from PIL import Image
                img = Image.open(logo_src).convert('RGB')
                img.save(temp_logo_jpg, format='JPEG')
                try:
                    pdf.image(temp_logo_jpg, x=12, y=8, w=30)
                    print(f"[PDF] Logo inserted for tx id={tx.id} (from static/logo.png)")
                except Exception as e:
                    print(f"[PDF] Logo insertion failed after conversion: {type(e).__name__}: {e}")
                try:
                    os.remove(temp_logo_jpg)
                except Exception:
                    pass
            except Exception as e:
                print(f"[PDF] Logo conversion/insertion failed: {type(e).__name__}: {e}")
                # fallback synthetic logo
                try:
                    from PIL import Image, ImageDraw, ImageFont
                    img = Image.new('RGB', (600, 180), (0, 110, 80))
                    d = ImageDraw.Draw(img)
                    try:
                        font = ImageFont.load_default()
                    except Exception:
                        font = None
                    d.text((20, 60), 'BNP PARIBAS', fill=(255, 255, 255), font=font)
                    img.save(temp_logo_jpg, format='JPEG')
                    try:
                        pdf.image(temp_logo_jpg, x=12, y=8, w=30)
                        print(f"[PDF] Logo inserted for tx id={tx.id} (synthetic)")
                    except Exception as e2:
                        print(f"[PDF] Fallback logo insertion failed: {type(e2).__name__}: {e2}")
                    try:
                        os.remove(temp_logo_jpg)
                    except Exception:
                        pass
                except Exception as e3:
                    print(f"[PDF] Synthetic logo creation failed: {type(e3).__name__}: {e3}")
        else:
            # No static logo — create synthetic one
            try:
                from PIL import Image, ImageDraw, ImageFont
                img = Image.new('RGB', (600, 180), (0, 110, 80))
                d = ImageDraw.Draw(img)
                try:
                    font = ImageFont.load_default()
                except Exception:
                    font = None
                d.text((20, 60), 'BNP PARIBAS', fill=(255, 255, 255), font=font)
                img.save(temp_logo_jpg, format='JPEG')
                try:
                    pdf.image(temp_logo_jpg, x=12, y=8, w=30)
                    print(f"[PDF] Logo inserted for tx id={tx.id} (no static)")
                except Exception as e:
                    print(f"[PDF] Synthetic logo insertion failed: {type(e).__name__}: {e}")
                try:
                    os.remove(temp_logo_jpg)
                except Exception:
                    pass
            except Exception as e:
                print(f"[PDF] Synthetic logo creation failed: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[PDF] Unexpected logo handling error: {type(e).__name__}: {e}")

    # En-tête
    pdf.set_font('Helvetica', 'B', 14)
    pdf.cell(0, 8, 'BNP PARIBAS', ln=True, align='C')
    pdf.set_font('Helvetica', '', 10)
    pdf.cell(0, 3, '=' * 70, ln=True, align='C')

    # Titre principal
    pdf.ln(4)
    pdf.set_font('Helvetica', 'B', 12)
    pdf.cell(0, 8, 'ATTESTATION DE VIREMENT', ln=True, align='C')
    
    # Date d'édition
    pdf.ln(3)
    pdf.set_font('Helvetica', '', 10)
    date_edition = tx.date.strftime('%d/%m/%Y à %H:%M') if tx.date else 'N/A'
    pdf.cell(0, 6, f'Date d\'édition : {date_edition}', ln=True)
    
    # Ligne de séparation
    pdf.set_font('Helvetica', '', 9)
    pdf.cell(0, 4, '-' * 70, ln=True)
    
    # Section COMPTE DÉBITÉ
    pdf.ln(2)
    pdf.set_font('Helvetica', 'B', 11)
    pdf.cell(0, 6, 'COMPTE DÉBITÉ', ln=True)
    pdf.set_font('Helvetica', '', 10)
    
    titulaire = f'{source_user.nom or ""} {source_user.prenom or ""}'.strip() if source_user else 'N/A'
    pdf.cell(0, 5, f'Titulaire : {titulaire}', ln=True)
    pdf.cell(0, 5, f'IBAN : {source_compte.iban if source_compte else "N/A"}', ln=True)
    
    # Section COMPTE CRÉDITÉ
    pdf.ln(3)
    pdf.set_font('Helvetica', 'B', 11)
    pdf.cell(0, 6, 'COMPTE CRÉDITÉ', ln=True)
    pdf.set_font('Helvetica', '', 10)
    
    beneficiaire_full = f'{tx.beneficiaire_nom or ""} {tx.beneficiaire_prenom or ""}'.strip() or 'N/A'
    pdf.cell(0, 5, f'Bénéficiaire : {beneficiaire_full}', ln=True)
    pdf.cell(0, 5, f'IBAN : {tx.beneficiaire_iban or "N/A"}', ln=True)
    pdf.cell(0, 5, f'BIC : {tx.beneficiaire_bic or "N/A"}', ln=True)
    
    # Section DÉTAILS DE L'OPÉRATION
    pdf.ln(3)
    pdf.set_font('Helvetica', 'B', 11)
    pdf.cell(0, 6, 'DÉTAILS DE L\'OPÉRATION', ln=True)
    pdf.set_font('Helvetica', '', 10)
    
    montant_fmt = f'{tx.montant:,.2f} EUR'.replace(',', ' ')
    pdf.cell(0, 5, f'Montant : {montant_fmt}', ln=True)
    
    date_exec = tx.date_execution_prevue.strftime('%d/%m/%Y') if tx.date_execution_prevue else tx.date.strftime('%d/%m/%Y') if tx.date else 'N/A'
    pdf.cell(0, 5, f'Date d\'exécution : {date_exec}', ln=True)
    
    # Nature
    nature = 'Virement SEPA instantané' if tx.virement_mode == 'instantane' else 'Virement SEPA standard'
    pdf.cell(0, 5, f'Nature : {nature}', ln=True)

    ref_op = tx.transaction_ref if getattr(tx, 'transaction_ref', None) else (f'OPE-{tx.date.strftime("%Y%m%d")}-{tx.id:08X}' if tx.date else f'OPE-{tx.id}')
    pdf.cell(0, 5, f'Référence de transaction : {ref_op}', ln=True)

    e2e = tx.end_to_end_ref or 'N/A'
    pdf.cell(0, 5, f'Référence de bout en bout : {e2e}', ln=True)
    # Génération d'un QR contenant les références et montant (si la lib est longue, tronquer)
    try:
        import qrcode
        qr_payload = f"Ref:{ref_op}\nE2E:{e2e}\nMontant:{montant_fmt}\nDate:{date_exec}"
        qr_img = qrcode.make(qr_payload)
        try:
            # Ensure RGB mode and save as PNG
            if hasattr(qr_img, 'convert'):
                qr_img = qr_img.convert('RGB')
        except Exception:
            pass
        qr_path = os.path.join(_instance, f'qr_{tx.id}.png')
        try:
            qr_img.save(qr_path)
            try:
                pdf.image(qr_path, x=160, y=12, w=30)
                print(f"[PDF] QR inserted for tx id={tx.id}")
            except Exception as e:
                print(f"[PDF] Failed to insert QR image {qr_path}: {type(e).__name__}: {e}")
        finally:
            try:
                if os.path.exists(qr_path):
                    os.remove(qr_path)
            except Exception:
                pass
    except Exception as e:
        print(f"[PDF] QR generation failed: {type(e).__name__}: {e}")

    libelle_court = tx.libelle[:80] if tx.libelle else 'N/A'
    pdf.cell(0, 5, f'Libellé : {libelle_court}', ln=True)
    
    # Pied de page
    pdf.ln(6)
    pdf.set_font('Helvetica', '', 8)
    pdf.cell(0, 3, '-' * 70, ln=True)
    pdf.cell(0, 4, 'BNP Paribas - SA au capital de 2 499 597 122 euros - Siège social : 16 boulevard des Italiens, 75009 Paris', ln=True)
    pdf.cell(0, 4, 'Immatriculée au RCS Paris sous le numéro 662 042 449.', ln=True)
    pdf.cell(0, 4, 'Document généré automatiquement à la demande du client.', ln=True)
    pdf.cell(0, 4, '=' * 70, ln=True)
    
    pdf_bytes = bytes(pdf.output(dest='S'))
    filename = f'recu_virement_{tx.id}.pdf'
    return Response(pdf_bytes, mimetype='application/pdf', headers={
        'Content-Disposition': f'attachment; filename="{filename}"'
    })


def setup_database():
    with app.app_context():
        # db.create_all() crée toutes les tables manquantes (compatible SQLite + PostgreSQL).
        # En cas de mise à jour du schéma, utilisez Flask-Migrate (alembic) pour les migrations.
        db.create_all()

        # Création de l'admin par défaut s'il n'existe pas
        if not User.query.filter_by(role='admin').first():
            # Le mot de passe est lu depuis la variable d'environnement ADMIN_PASSWORD.
            # Si elle n'est pas définie, un mot de passe aléatoire sécurisé est généré
            # et affiché dans les logs de démarrage (une seule fois).
            _admin_pw = os.environ.get('ADMIN_PASSWORD') or ''.join(
                random.choices(string.ascii_letters + string.digits + '!@#$%', k=16)
            )
            admin = User(
                num_client='ADMIN001',
                prenom='Admin',
                nom='BNP Paribas',
                date_naissance='01/01/1970',
                adresse='Siège BNP Paribas',
                role='admin'
            )
            admin.set_password(_admin_pw)
            db.session.add(admin)
            db.session.commit()
            print(f"[SECURITE] Administrateur créé — N° client : ADMIN001 — Mot de passe initial : {_admin_pw}")
            print("[SECURITE] Notez ce mot de passe, il ne sera plus affiché.")
            print("[SECURITE] Définissez la variable d'environnement ADMIN_PASSWORD pour le fixer.")

setup_database()

if __name__ == '__main__':
    # Démarrage local (python app.py)
    try:
        import backup_user_data
        backup_user_data.backup_db(os.path.join(_basedir, 'app.db'))
        backup_user_data.backup_db(os.path.join(_instance, 'bnpparibas.db'))
    except Exception as e:
        print(f"[Backup] Échec sauvegarde : {e}")
    port = int(os.environ.get('PORT', 5001))
    print(f"[BNP Paribas] http://127.0.0.1:{port}")
    app.run(debug=False, host='0.0.0.0', port=port, threaded=True, use_reloader=False)
