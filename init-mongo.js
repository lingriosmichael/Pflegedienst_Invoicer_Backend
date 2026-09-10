const admin = db.getSiblingDB("admin");
const username = process.env.MONGO_APP_USER;
const password = process.env.MONGO_APP_PASSWORD;
const databaseName = process.env.MONGO_INITDB_DATABASE || "pflegedienst_db";
if (!username || !password) throw new Error("Application credentials are required");
if (!admin.getUser(username)) {
    admin.createUser({ user: username, pwd: password, roles: [{ role: "readWrite", db: databaseName }] });
}
const application = db.getSiblingDB(databaseName);
const validators = JSON.parse(require("fs").readFileSync("/docker-entrypoint-initdb.d/validators.json", "utf8"));
for (const [name, validator] of Object.entries(validators)) {
    if (!application.getCollectionNames().includes(name)) application.createCollection(name, { validator });
}
