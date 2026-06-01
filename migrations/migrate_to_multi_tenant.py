"""
Migration Script: Convert Existing Users to Multi-Tenant Structure

This script migrates existing single-tenant users to the new multi-tenant architecture:
1. Creates a default client for each existing user
2. Creates a default workspace under each client
3. Adds the user as owner of their default workspace
4. Assigns existing user data (content_drafts, etc.) to their default workspace

Run with: python migrations/migrate_to_multi_tenant.py [--dry-run]
"""

import asyncio
import sys
import os
from datetime import datetime
from typing import Dict, List
from motor.motor_asyncio import AsyncIOMotorClient

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.models.client import Client, ClientBillingInfo, ClientSubscription
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember, WorkspaceRole, WorkspacePermissions
from app.core.config import settings


class MultiTenantMigration:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.client: AsyncIOMotorClient = None
        self.db = None

        # Statistics
        self.stats = {
            "users_processed": 0,
            "clients_created": 0,
            "workspaces_created": 0,
            "members_added": 0,
            "content_drafts_migrated": 0,
            "content_requests_migrated": 0,
            "brand_profiles_migrated": 0,
            "social_connections_migrated": 0,
            "errors": [],
        }

    async def connect(self):
        """Connect to MongoDB"""
        print(f"Connecting to MongoDB: {settings.MONGODB_URI}")
        self.client = AsyncIOMotorClient(settings.MONGODB_URI)
        self.db = self.client[settings.MONGODB_DB]

        # Test connection
        await self.db.command("ping")
        print("✅ Connected to MongoDB\n")

    async def close(self):
        """Close MongoDB connection"""
        if self.client:
            self.client.close()
            print("\n✅ Closed MongoDB connection")

    async def get_existing_users(self) -> List[Dict]:
        """Get all existing users"""
        print("📋 Fetching existing users...")

        users = await self.db.users.find({}).to_list(length=None)
        print(f"Found {len(users)} users\n")

        return users

    async def user_has_client(self, user_id: str) -> bool:
        """Check if user already has a client"""
        existing_client = await self.db.clients.find_one({"owner_user_id": user_id})
        return existing_client is not None

    async def create_default_client(self, user: Dict) -> Client:
        """Create a default client for a user"""
        user_id = user.get("userId")
        email = user.get("email", "")
        full_name = user.get("full_name", "")
        first_name = user.get("first_name", "User")
        last_name = user.get("last_name", "")

        # Generate client name
        if full_name:
            client_name = f"{full_name}'s Account"
        elif first_name and last_name:
            client_name = f"{first_name} {last_name}'s Account"
        else:
            client_name = f"User {user_id[:8]}'s Account"

        # Generate unique slug
        base_slug = client_name.lower().replace("'s account", "").replace(" ", "-")
        base_slug = ''.join(c for c in base_slug if c.isalnum() or c == '-')

        slug = base_slug
        counter = 1
        while await self.db.clients.find_one({"slug": slug}):
            slug = f"{base_slug}-{counter}"
            counter += 1

        # Create client
        client = Client(
            client_id=Client.generate_client_id(),
            name=client_name,
            slug=slug,
            description=f"Default client account for {email}",
            owner_user_id=user_id,
            billing_info=ClientBillingInfo(
                billing_email=email,
                billing_name=full_name or f"{first_name} {last_name}".strip() or email,
            ),
            subscription=ClientSubscription(
                tier="starter",  # Default to starter tier
                total_credits=1000,  # Give initial credits
            )
        )

        if not self.dry_run:
            result = await self.db.clients.insert_one(client.to_dict())
            client.id = str(result.inserted_id)
            self.stats["clients_created"] += 1

        return client

    async def create_default_workspace(self, client: Client, user: Dict) -> Workspace:
        """Create a default workspace for a client"""
        workspace_name = "My Workspace"

        # Generate unique slug
        slug = "my-workspace"
        counter = 1
        while await self.db.workspaces.find_one({"client_id": client.client_id, "slug": slug}):
            slug = f"my-workspace-{counter}"
            counter += 1

        # Create workspace
        workspace = Workspace(
            workspace_id=Workspace.generate_workspace_id(),
            client_id=client.client_id,
            name=workspace_name,
            slug=slug,
            description="Default workspace",
            created_by_user_id=user.get("userId"),
        )

        if not self.dry_run:
            result = await self.db.workspaces.insert_one(workspace.to_dict())
            workspace.id = str(result.inserted_id)
            self.stats["workspaces_created"] += 1

        return workspace

    async def add_user_as_owner(self, workspace: Workspace, user_id: str):
        """Add user as owner of workspace"""
        member = WorkspaceMember(
            workspace_id=workspace.workspace_id,
            user_id=user_id,
            role=WorkspaceRole.OWNER,
            permissions=WorkspacePermissions.for_role(WorkspaceRole.OWNER),
            invited_by_user_id=None,  # Self-added
            status="active",
        )

        if not self.dry_run:
            await self.db.workspace_members.insert_one(member.to_dict())
            self.stats["members_added"] += 1

    async def migrate_user_data(self, user_id: str, workspace_id: str):
        """Migrate user's existing data to workspace"""

        # Migrate content_drafts
        if not self.dry_run:
            result = await self.db.content_drafts.update_many(
                {"user_id": user_id, "workspace_id": {"$exists": False}},
                {"$set": {"workspace_id": workspace_id, "updated_at": datetime.utcnow()}}
            )
            self.stats["content_drafts_migrated"] += result.modified_count

        # Migrate content_requests
        if not self.dry_run:
            result = await self.db.content_requests.update_many(
                {"user_id": user_id, "workspace_id": {"$exists": False}},
                {"$set": {"workspace_id": workspace_id, "updated_at": datetime.utcnow()}}
            )
            self.stats["content_requests_migrated"] += result.modified_count

        # Migrate brand_profiles
        if not self.dry_run:
            result = await self.db.brand_profiles.update_many(
                {"user_id": user_id, "workspace_id": {"$exists": False}},
                {"$set": {"workspace_id": workspace_id, "updated_at": datetime.utcnow()}}
            )
            self.stats["brand_profiles_migrated"] += result.modified_count

        # Migrate social_connections
        if not self.dry_run:
            result = await self.db.social_connections.update_many(
                {"user_id": user_id, "workspace_id": {"$exists": False}},
                {"$set": {"workspace_id": workspace_id, "updated_at": datetime.utcnow()}}
            )
            self.stats["social_connections_migrated"] += result.modified_count

    async def migrate_user(self, user: Dict):
        """Migrate a single user to multi-tenant structure"""
        user_id = user.get("userId")
        email = user.get("email", "unknown")

        try:
            print(f"  Processing user: {email} ({user_id})")

            # Check if user already has a client
            if await self.user_has_client(user_id):
                print(f"    ⏭️  User already has a client, skipping")
                return

            # Create default client
            print(f"    ➕ Creating default client...")
            client = await self.create_default_client(user)
            print(f"       Client ID: {client.client_id}")

            # Create default workspace
            print(f"    ➕ Creating default workspace...")
            workspace = await self.create_default_workspace(client, user)
            print(f"       Workspace ID: {workspace.workspace_id}")

            # Add user as owner
            print(f"    ➕ Adding user as workspace owner...")
            await self.add_user_as_owner(workspace, user_id)

            # Migrate existing data
            print(f"    🔄 Migrating user's existing data...")
            await self.migrate_user_data(user_id, workspace.workspace_id)

            self.stats["users_processed"] += 1
            print(f"    ✅ User migration complete\n")

        except Exception as e:
            error_msg = f"Error migrating user {email}: {str(e)}"
            print(f"    ❌ {error_msg}\n")
            self.stats["errors"].append(error_msg)

    async def run(self):
        """Run the migration"""
        print("=" * 80)
        print("MULTI-TENANT MIGRATION SCRIPT")
        print("=" * 80)

        if self.dry_run:
            print("🔍 DRY RUN MODE - No changes will be made\n")
        else:
            print("⚠️  LIVE MODE - Changes will be written to database\n")

        try:
            # Connect to database
            await self.connect()

            # Get all existing users
            users = await self.get_existing_users()

            if not users:
                print("No users found. Migration complete.")
                return

            # Migrate each user
            print("🚀 Starting user migration...\n")

            for user in users:
                await self.migrate_user(user)

            # Print summary
            self.print_summary()

        except Exception as e:
            print(f"\n❌ Migration failed: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await self.close()

    def print_summary(self):
        """Print migration summary"""
        print("\n" + "=" * 80)
        print("MIGRATION SUMMARY")
        print("=" * 80)

        if self.dry_run:
            print("🔍 DRY RUN - No changes were made\n")
        else:
            print("✅ MIGRATION COMPLETE\n")

        print(f"Users processed:             {self.stats['users_processed']}")
        print(f"Clients created:             {self.stats['clients_created']}")
        print(f"Workspaces created:          {self.stats['workspaces_created']}")
        print(f"Members added:               {self.stats['members_added']}")
        print(f"Content drafts migrated:     {self.stats['content_drafts_migrated']}")
        print(f"Content requests migrated:   {self.stats['content_requests_migrated']}")
        print(f"Brand profiles migrated:     {self.stats['brand_profiles_migrated']}")
        print(f"Social connections migrated: {self.stats['social_connections_migrated']}")

        if self.stats["errors"]:
            print(f"\n⚠️  Errors encountered: {len(self.stats['errors'])}")
            for error in self.stats["errors"]:
                print(f"  - {error}")
        else:
            print("\n✅ No errors encountered")

        print("=" * 80)


async def main():
    """Main entry point"""
    # Check for dry-run flag
    dry_run = "--dry-run" in sys.argv

    migration = MultiTenantMigration(dry_run=dry_run)
    await migration.run()


if __name__ == "__main__":
    asyncio.run(main())
