from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
import boto3
from botocore.client import Config
import os
from datetime import datetime
from dotenv import load_dotenv

# Load .env for local, Railway uses its own env vars
load_dotenv()

app = Flask(__name__)

# ------------------ Database Config ------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL is not set in environment variables")

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ------------------ Supabase S3 Config ------------------
S3_ENDPOINT = os.getenv("S3_ENDPOINT")
S3_REGION = os.getenv("S3_REGION", "us-east-2")
S3_BUCKET = os.getenv("S3_BUCKET")
S3_KEY = os.getenv("S3_KEY")
S3_SECRET = os.getenv("S3_SECRET")

if not all([S3_ENDPOINT, S3_BUCKET, S3_KEY, S3_SECRET]):
    raise Exception("One or more S3 environment variables are missing")

s3 = boto3.client(
    "s3",
    region_name=S3_REGION,
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_KEY,
    aws_secret_access_key=S3_SECRET,
    config=Config(signature_version="s3v4")
)

# ------------------ Models ------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(15), nullable=False)
    aadhaar = db.Column(db.String(12), unique=True, nullable=True)
    email = db.Column(db.String(120), nullable=True)
    address = db.Column(db.Text, nullable=True)  # <-- new field
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)


class Document(db.Model):
    __tablename__ = "documents"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    doc_type = db.Column(db.String(20), nullable=False)  # aadhaar, pan, dl, voterid
    doc_url = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), default="pending")  # pending, verified, rejected
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


class Face(db.Model):
    __tablename__ = "faces"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    face_url = db.Column(db.String(255), nullable=False)
    liveness_score = db.Column(db.Float, nullable=True)
    match_score = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ------------------ Helper ------------------
def upload_to_supabase(file, path):
    filename = secure_filename(file.filename)
    key = f"{path}/{filename}"
    s3.upload_fileobj(
        Fileobj=file,
        Bucket=S3_BUCKET,
        Key=key,
        ExtraArgs={"ContentType": file.mimetype}
    )
    # Build public URL
    return f"{S3_ENDPOINT.replace('/s3','')}/object/public/{S3_BUCKET}/{key}"


# ------------------ Routes ------------------
@app.route('/')
def home():
    return jsonify({"message": "KYC API with Users, Documents & Faces ✅"})


# ---- USERS ----
@app.route('/users', methods=['POST'])
def create_user():
    data = request.json
    user = User(name=data['name'], phone=data['phone'], aadhaar=data.get('aadhaar'))
    db.session.add(user)
    db.session.commit()
    return jsonify({"status": "success", "user_id": user.id}), 201


@app.route('/users/<int:id>', methods=['GET'])
def get_user(id):
    user = User.query.get(id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "aadhaar": user.aadhaar,
        "created_at": user.created_at
    })


@app.route('/users/<int:id>', methods=['PUT'])
def update_user(id):
    user = User.query.get(id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    data = request.json
    if "phone" in data: user.phone = data["phone"]
    if "name" in data: user.name = data["name"]
    if "aadhaar" in data: user.aadhaar = data["aadhaar"]
    db.session.commit()
    return jsonify({"status": "updated"}), 200


@app.route('/users/<int:id>', methods=['DELETE'])
def delete_user(id):
    user = User.query.get(id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    db.session.delete(user)
    db.session.commit()
    return jsonify({"status": "deleted"}), 200


# ---- DOCUMENTS ----
@app.route('/users/<int:id>/documents', methods=['POST'])
def upload_document(id):
    user = User.query.get(id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    if 'file' not in request.files or 'doc_type' not in request.form:
        return jsonify({"error": "File and doc_type required"}), 400
    file = request.files['file']
    doc_type = request.form['doc_type']
    url = upload_to_supabase(file, f"documents/{id}/{doc_type}")
    doc = Document(user_id=id, doc_type=doc_type, doc_url=url)
    db.session.add(doc)
    db.session.commit()
    return jsonify({"status": "uploaded", "doc_id": doc.id, "doc_url": url}), 201


@app.route('/users/<int:id>/documents', methods=['GET'])
def list_documents(id):
    docs = Document.query.filter_by(user_id=id).all()
    return jsonify([{
        "id": d.id,
        "doc_type": d.doc_type,
        "doc_url": d.doc_url,
        "status": d.status,
        "uploaded_at": d.uploaded_at
    } for d in docs]), 200


@app.route('/documents/<int:doc_id>', methods=['DELETE'])
def delete_document(doc_id):
    doc = Document.query.get(doc_id)
    if not doc:
        return jsonify({"error": "Document not found"}), 404
    db.session.delete(doc)
    db.session.commit()
    return jsonify({"status": "deleted"}), 200


# ---- FACES ----
@app.route('/users/<int:id>/face', methods=['POST'])
def upload_face(id):
    user = User.query.get(id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    if 'file' not in request.files:
        return jsonify({"error": "File required"}), 400
    file = request.files['file']
    url = upload_to_supabase(file, f"faces/{id}")
    face = Face(user_id=id, face_url=url)
    db.session.add(face)
    db.session.commit()
    return jsonify({"status": "uploaded", "face_id": face.id, "face_url": url}), 201


@app.route('/users/<int:id>/face', methods=['GET'])
def get_faces(id):
    faces = Face.query.filter_by(user_id=id).all()
    return jsonify([{
        "id": f.id,
        "face_url": f.face_url,
        "liveness_score": f.liveness_score,
        "match_score": f.match_score,
        "status": f.status,
        "created_at": f.created_at
    } for f in faces]), 200


@app.route('/faces/<int:face_id>', methods=['DELETE'])
def delete_face(face_id):
    face = Face.query.get(face_id)
    if not face:
        return jsonify({"error": "Face not found"}), 404
    db.session.delete(face)
    db.session.commit()
    return jsonify({"status": "deleted"}), 200


# ------------------ Ensure Tables Exist ------------------
with app.app_context():
    db.create_all()


# ------------------ Main ------------------
if __name__ == "__main__":
    print("Tables created ✅")
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
