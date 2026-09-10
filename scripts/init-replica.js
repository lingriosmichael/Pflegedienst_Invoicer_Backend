const username = encodeURIComponent(process.env.MONGO_ROOT_USER);
const password = encodeURIComponent(process.env.MONGO_ROOT_PASSWORD);
const connection = new Mongo(`mongodb://${username}:${password}@mongodb:27017/admin?directConnection=true`);
const admin = connection.getDB("admin");
try {
    const status = admin.runCommand({ replSetGetStatus: 1 });
    if (status.code === 94) {
        const result = admin.runCommand({ replSetInitiate: { _id: "rs0", members: [{ _id: 0, host: "mongodb:27017" }] } });
        if (!result.ok) throw new Error("Replica set initialization failed");
    } else if (!status.ok) throw new Error("Replica set status unavailable");
} catch (error) {
    if (error.code !== 94) throw error;
    const result = admin.runCommand({ replSetInitiate: { _id: "rs0", members: [{ _id: 0, host: "mongodb:27017" }] } });
    if (!result.ok) throw new Error("Replica set initialization failed");
}
let ready = false;
for (let attempt = 0; attempt < 60; attempt++) {
    if (admin.runCommand({ hello: 1 }).isWritablePrimary) { ready = true; break; }
    sleep(1000);
}
if (!ready) throw new Error("Replica set did not elect a primary");
