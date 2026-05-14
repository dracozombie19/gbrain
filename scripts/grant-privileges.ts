import postgres from 'postgres';

// Connect as postgres (superuser) to grant BYPASSRLS and full privileges to gbrain user
const sql = postgres('postgres://postgres:y86kKGJX8Tda27FRZWeuAtR+@34.57.122.49:5432/gbrain', {
  ssl: 'require',
  max: 1,
  connect_timeout: 30,
});

console.log('Connecting as postgres superuser...');

try {
  // Grant BYPASSRLS so migrations with RLS succeed
  await sql`ALTER USER gbrain WITH BYPASSRLS`;
  console.log('✅ Granted BYPASSRLS to gbrain');

  // Grant all existing table/sequence privileges
  await sql`GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO gbrain`;
  await sql`GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO gbrain`;
  await sql`GRANT ALL PRIVILEGES ON SCHEMA public TO gbrain`;
  console.log('✅ Granted ALL PRIVILEGES on existing objects');

  // Make future objects also accessible
  await sql`ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO gbrain`;
  await sql`ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO gbrain`;
  console.log('✅ Set default privileges for future objects');

  console.log('\n✅ All done! The gbrain user now has full access including BYPASSRLS.');
} catch (err) {
  console.error('Error:', err);
  process.exit(1);
} finally {
  await sql.end();
}
