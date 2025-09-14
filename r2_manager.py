import os
import json
import boto3
from dotenv import load_dotenv
from botocore.exceptions import ClientError

load_dotenv()

R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_ENDPOINT = os.getenv("R2_ENDPOINT")

session = boto3.session.Session()
s3 = session.client(
    service_name="s3",
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    endpoint_url=R2_ENDPOINT,
)

def load_songs():
    try:
        response = s3.get_object(Bucket=R2_BUCKET_NAME, Key="songs.json")
        return json.load(response["Body"])
    except ClientError as e:
        print(f"⚠️ 無法讀取 songs.json：{e}")
        return []

def load_playlist(user_id: str):
    key = f"playlists/{user_id}.json"
    try:
        response = s3.get_object(Bucket=R2_BUCKET_NAME, Key=key)
        return json.load(response["Body"])
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            return {}
        print(f"⚠️ 無法讀取 {key}：{e}")
        return {}

def save_playlist(user_id: str, playlists: dict):
    key = f"playlists/{user_id}.json"
    body = json.dumps(playlists, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        s3.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=body, ContentType="application/json")
        print(f"✅ 已更新：{key}")
    except ClientError as e:
        print(f"❌ 無法上傳 {key}：{e}")
