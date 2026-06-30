from pymongo import MongoClient

uri = "mongodb+srv://by8tech:ChangePassword%4012@cluster0.uhfwy.mongodb.net/by8flow"
client = MongoClient(uri)
db = client["by8flow"]

# Delete all oauth_states (they're one-time use and expired)
result = db["oauth_states"].delete_many({})
print(f"Deleted {result.deleted_count} oauth_states documents")

# Confirm oauth_tokens is empty
token_doc = db["oauth_tokens"].find_one({"_id": "google_calendar"})
if token_doc:
    print("WARNING: token document still exists. Delete it? (check code to confirm)")
else:
    print("No token document — clean state")
