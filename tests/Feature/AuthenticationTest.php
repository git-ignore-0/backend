<?php

namespace Tests\Feature;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

class AuthenticationTest extends TestCase
{
    use RefreshDatabase;

    public function test_active_user_can_log_in(): void
    {
        $user = User::factory()->create(['password' => 'StrongPassword12']);

        $this->post('/login', [
            'email' => $user->email,
            'password' => 'StrongPassword12',
        ])->assertRedirect('/account');

        $this->assertAuthenticatedAs($user);
        $this->assertDatabaseHas('audit_logs', ['user_id' => $user->id, 'action' => 'auth.login']);
    }

    public function test_suspended_user_cannot_log_in(): void
    {
        $user = User::factory()->suspended()->create(['password' => 'StrongPassword12']);

        $this->from('/login')->post('/login', [
            'email' => $user->email,
            'password' => 'StrongPassword12',
        ])->assertRedirect('/login')->assertSessionHasErrors('email');

        $this->assertGuest();
    }

    public function test_pending_user_is_sent_to_email_verification(): void
    {
        $user = User::factory()->unverified()->create();

        $this->actingAs($user)->get('/account')->assertRedirect('/email/verify');
    }
}
