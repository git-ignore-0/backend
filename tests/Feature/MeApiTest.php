<?php

namespace Tests\Feature;

use App\Models\Role;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Laravel\Passport\Passport;
use Tests\TestCase;

class MeApiTest extends TestCase
{
    use RefreshDatabase;

    public function test_profile_scope_returns_stable_core_identity(): void
    {
        $user = User::factory()->create(['locale' => 'vi']);
        Role::create(['name' => 'member', 'description' => 'Standard member']);
        $user->roles()->attach('member');
        Passport::actingAs($user, ['profile', 'email']);

        $this->getJson('/api/v1/me')
            ->assertOk()
            ->assertJsonPath('data.sub', $user->id)
            ->assertJsonPath('data.email', $user->email)
            ->assertJsonPath('data.status', 'active')
            ->assertJsonPath('data.roles.0', 'member');
    }

    public function test_suspended_identity_is_rejected_even_with_a_valid_token(): void
    {
        $user = User::factory()->suspended()->create();
        Passport::actingAs($user, ['profile', 'email']);

        $this->getJson('/api/v1/me')
            ->assertForbidden()
            ->assertJsonPath('code', 'account_inactive');
    }

    public function test_profile_scope_is_required(): void
    {
        $user = User::factory()->create();
        Passport::actingAs($user, []);

        $this->getJson('/api/v1/me')->assertForbidden();
    }
}
