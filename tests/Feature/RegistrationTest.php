<?php

namespace Tests\Feature;

use App\Enums\AccountStatus;
use App\Models\User;
use Illuminate\Auth\Events\Verified;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Facades\Notification;
use Tests\TestCase;

class RegistrationTest extends TestCase
{
    use RefreshDatabase;

    public function test_registration_creates_a_pending_uuid_identity_and_requests_verification(): void
    {
        Notification::fake();

        $response = $this->post('/register', [
            'name' => '  Nông Dân Xanh  ',
            'email' => 'HELLO@example.com',
            'locale' => 'vi',
            'password' => 'StrongPassword12',
            'password_confirmation' => 'StrongPassword12',
        ]);

        $user = User::firstOrFail();

        $response->assertRedirect('/account');
        $this->assertSame('Nông Dân Xanh', $user->name);
        $this->assertSame('hello@example.com', $user->email);
        $this->assertSame(AccountStatus::PendingVerification, $user->status);
        $this->assertNull($user->email_verified_at);
        $this->assertTrue((bool) preg_match('/^[0-9a-f-]{36}$/', $user->id));
    }

    public function test_verified_event_activates_the_identity_and_creates_outbox_event(): void
    {
        Event::fakeExcept(Verified::class);
        $user = User::factory()->unverified()->create();
        $user->markEmailAsVerified();

        event(new Verified($user));

        $this->assertSame(AccountStatus::Active, $user->fresh()->status);
        $this->assertDatabaseHas('identity_events', [
            'user_id' => $user->id,
            'type' => 'user.activated',
            'status' => 'pending',
        ]);
    }
}
