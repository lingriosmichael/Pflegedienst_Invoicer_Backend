// MongoDB initialization script
// This runs automatically when MongoDB starts for the first time.
//
// ⚠️  LOCAL DEVELOPMENT ONLY — these are default credentials for Docker.
//     Override MONGO_APP_USER / MONGO_APP_PASSWORD via your .env file and
//     update the credentials below before deploying to any shared environment.

// Switch to admin database
db = db.getSiblingDB('admin');

// Create application user (if not already created)
if (!db.getUser('pflegedienst_user')) {
    db.createUser({
        user: 'pflegedienst_user',
        pwd: 'pflegedienst_password',
        roles: [
            {
                role: 'readWrite',
                db: 'pflegedienst_invoicer'
            }
        ]
    });
    print('✓ Created application user: pflegedienst_user');
} else {
    print('ℹ Application user already exists');
}

// Switch to application database
db = db.getSiblingDB('pflegedienst_invoicer');

// Create collections with validators
// This ensures data structure consistency

// Collection 1: patient_profiles
try {
    db.createCollection('patient_profiles', {
        validator: {
            $jsonSchema: {
                bsonType: 'object',
                required: ['patient_id', 'org_id', 'patient_name', 'insurance_number'],
                properties: {
                    _id: { bsonType: 'objectId' },
                    patient_id: { bsonType: 'string', description: 'Unique identifier' },
                    org_id: { bsonType: 'string', default: 'org_default' },
                    patient_name: { bsonType: 'string' },
                    insurance_number: { bsonType: 'string' },
                    care_level: { bsonType: 'string' },
                    date_of_birth: { bsonType: 'string' },
                    street_name: { bsonType: 'string' },
                    street_number: { bsonType: 'string' },
                    postal_code: { bsonType: 'string' },
                    city: { bsonType: 'string' },
                    debtor_id: { bsonType: 'string' },
                    include_service_packet: { bsonType: 'bool' },
                    created_at: { bsonType: 'date' },
                    updated_at: { bsonType: 'date' }
                }
            }
        }
    });
    print('✓ Created collection: patient_profiles');
} catch (e) {
    print('ℹ Collection patient_profiles already exists: ' + e.message);
}

// Collection 2: care_events
try {
    db.createCollection('care_events', {
        validator: {
            $jsonSchema: {
                bsonType: 'object',
                required: ['care_event_id', 'org_id', 'patient_id', 'event_type'],
                properties: {
                    _id: { bsonType: 'objectId' },
                    care_event_id: { bsonType: 'string' },
                    org_id: { bsonType: 'string', default: 'org_default' },
                    patient_id: { bsonType: 'string' },
                    event_type: { enum: ['SGBXI', 'Entleistung', 'SGBV', 'Verhinderungspflege'] },
                    period_start_date: { bsonType: 'string' },
                    period_end_date: { bsonType: 'string' },
                    care_account: { bsonType: 'string' },
                    sum_covered: { bsonType: 'double' },
                    sum_total: { bsonType: 'double' },
                    services: { bsonType: 'array' },
                    created_at: { bsonType: 'date' },
                    updated_at: { bsonType: 'date' }
                }
            }
        }
    });
    print('✓ Created collection: care_events');
} catch (e) {
    print('ℹ Collection care_events already exists');
}

// Collection 3: billing_details
try {
    db.createCollection('billing_details', {
        validator: {
            $jsonSchema: {
                bsonType: 'object',
                required: ['billing_detail_id', 'org_id', 'care_event_id'],
                properties: {
                    _id: { bsonType: 'objectId' },
                    billing_detail_id: { bsonType: 'string' },
                    org_id: { bsonType: 'string', default: 'org_default' },
                    care_event_id: { bsonType: 'string' },
                    invoicing_month: { bsonType: 'string' },
                    sum_covered: { bsonType: 'double' },
                    sum_total: { bsonType: 'double' },
                    investitionskosten: { bsonType: 'double' },
                    amount_owed: { bsonType: 'double' },
                    invoice_number: { bsonType: 'int' },
                    billing_status: { enum: ['invoice_needed', 'covered_insurance'] },
                    payment_received_date: { bsonType: 'date' },
                    created_at: { bsonType: 'date' },
                    updated_at: { bsonType: 'date' }
                }
            }
        }
    });
    print('✓ Created collection: billing_details');
} catch (e) {
    print('ℹ Collection billing_details already exists');
}

// Collection 4: billing_summary
try {
    db.createCollection('billing_summary', {
        validator: {
            $jsonSchema: {
                bsonType: 'object',
                required: ['summary_id', 'org_id', 'billing_month'],
                properties: {
                    _id: { bsonType: 'objectId' },
                    summary_id: { bsonType: 'string' },
                    org_id: { bsonType: 'string', default: 'org_default' },
                    billing_month: { bsonType: 'string' },
                    submitted_invoice_count: { bsonType: 'int' },
                    submitted_invoice_amount: { bsonType: 'double' },
                    created_at: { bsonType: 'date' },
                    updated_at: { bsonType: 'date' }
                }
            }
        }
    });
    print('✓ Created collection: billing_summary');
} catch (e) {
    print('ℹ Collection billing_summary already exists');
}

// Collection 5: care_event_history
try {
    db.createCollection('care_event_history', {
        validator: {
            $jsonSchema: {
                bsonType: 'object',
                required: ['history_id', 'org_id', 'care_event_id', 'action'],
                properties: {
                    _id: { bsonType: 'objectId' },
                    history_id: { bsonType: 'string' },
                    org_id: { bsonType: 'string', default: 'org_default' },
                    care_event_id: { bsonType: 'string' },
                    action: { enum: ['created', 'billing_generated'] },
                    created_at: { bsonType: 'date' }
                }
            }
        }
    });
    print('✓ Created collection: care_event_history');
} catch (e) {
    print('ℹ Collection care_event_history already exists');
}

// Collection 6: entlastungsleistung_tracking
try {
    db.createCollection('entlastungsleistung_tracking', {
        validator: {
            $jsonSchema: {
                bsonType: 'object',
                required: ['tracking_id', 'org_id', 'patient_id', 'calendar_year'],
                properties: {
                    _id: { bsonType: 'objectId' },
                    tracking_id: { bsonType: 'string' },
                    org_id: { bsonType: 'string', default: 'org_default' },
                    patient_id: { bsonType: 'string' },
                    calendar_year: { bsonType: 'int' },
                    cumulative_amount: { bsonType: 'double' },
                    last_updated: { bsonType: 'date' }
                }
            }
        }
    });
    print('✓ Created collection: entlastungsleistung_tracking');
} catch (e) {
    print('ℹ Collection entlastungsleistung_tracking already exists');
}

// Collection 7: invoice_sequences
try {
    db.createCollection('invoice_sequences', {
        validator: {
            $jsonSchema: {
                bsonType: 'object',
                required: ['org_id'],
                properties: {
                    _id: { bsonType: 'objectId' },
                    org_id: { bsonType: 'string', default: 'org_default' },
                    last_number: { bsonType: 'int' }
                }
            }
        }
    });
    print('✓ Created collection: invoice_sequences');
} catch (e) {
    print('ℹ Collection invoice_sequences already exists');
}

print('\n✓ All collections ready!');
