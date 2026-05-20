#!/usr/bin/env node
/**
 * Extract all users from MongoDB database
 * Outputs: CSV file with name, email, registration date
 * READ-ONLY - Safe to run, doesn't modify anything
 */

const { MongoClient } = require('mongodb');
const fs = require('fs');

// MongoDB connection from environment variable
const MONGODB_URI = process.env.MONGODB_URI;
const MONGODB_DB = process.env.MONGODB_DB || 'uri_social';

console.log(`📌 Connecting to database: ${MONGODB_DB}`);

async function extractUsers() {
  let client;

  try {
    // Connect to MongoDB
    client = new MongoClient(MONGODB_URI);
    await client.connect();
    console.log('✅ Connected to MongoDB');

    const db = client.db(MONGODB_DB);
    const usersCollection = db.collection('users');

    console.log('📥 Fetching users...');
    const users = await usersCollection.find({}).toArray();

    console.log(`✅ Found ${users.length} users`);

    // Extract and format user data
    const userData = users.map(user => {
      let registeredAt = user.createdAt || user.created_at || 'N/A';

      // Format date if it's a Date object
      if (registeredAt instanceof Date) {
        registeredAt = registeredAt.toISOString().replace('T', ' ').split('.')[0];
      }

      return {
        name: `${user.firstName || ''} ${user.lastName || ''}`.trim() || user.name || 'N/A',
        email: user.email || 'N/A',
        registered_at: registeredAt
      };
    });

    // Sort by registration date (newest first)
    userData.sort((a, b) => {
      if (a.registered_at === 'N/A') return 1;
      if (b.registered_at === 'N/A') return -1;
      return b.registered_at.localeCompare(a.registered_at);
    });

    // Generate CSV
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').split('T').join('_').substring(0, 19);
    const outputFile = `users_export_${timestamp}.csv`;

    // Create CSV content
    let csvContent = 'Name,Email,Registered At\n';
    userData.forEach(user => {
      const name = `"${user.name.replace(/"/g, '""')}"`;
      const email = `"${user.email.replace(/"/g, '""')}"`;
      const date = `"${user.registered_at}"`;
      csvContent += `${name},${email},${date}\n`;
    });

    // Write to file
    fs.writeFileSync(outputFile, csvContent, 'utf8');

    console.log(`\n✅ Users exported to: ${outputFile}`);
    console.log(`📊 Total users: ${users.length}`);

    // Print summary
    console.log('\n📋 Summary (first 10):');
    console.log('Name'.padEnd(30) + 'Email'.padEnd(35) + 'Registered');
    console.log('-'.repeat(85));

    userData.slice(0, 10).forEach(user => {
      console.log(
        user.name.padEnd(30).substring(0, 30) +
        user.email.padEnd(35).substring(0, 35) +
        user.registered_at
      );
    });

    if (userData.length > 10) {
      console.log(`\n... and ${userData.length - 10} more users`);
    }

    console.log(`\n✅ Done! File saved: ${outputFile}`);

  } catch (error) {
    console.error('❌ Error:', error.message);
    process.exit(1);
  } finally {
    if (client) {
      await client.close();
    }
  }
}

// Run the extraction
extractUsers();
