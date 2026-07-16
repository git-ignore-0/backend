<?php

namespace App\Providers;

use App\Enums\AccountStatus;
use App\Models\AuditLog;
use App\Models\IdentityEvent;
use App\Models\User;
use Illuminate\Auth\Events\Login;
use Illuminate\Auth\Events\Verified;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\ServiceProvider;
use Illuminate\Validation\Rules\Password;
use Laravel\Passport\Passport;

class AppServiceProvider extends ServiceProvider
{
    /**
     * Register any application services.
     */
    public function register(): void
    {
        //
    }

    /**
     * Bootstrap any application services.
     */
    public function boot(): void
    {
        Password::defaults(fn () => Password::min(12)->letters()->mixedCase()->numbers());

        Passport::tokensCan([
            'profile' => 'Read your basic profile',
            'email' => 'Read your email address',
        ]);
        Passport::tokensExpireIn(now()->addMinutes((int) env('PASSPORT_ACCESS_TOKEN_TTL', 60)));
        Passport::refreshTokensExpireIn(now()->addDays((int) env('PASSPORT_REFRESH_TOKEN_TTL', 30)));
        Passport::authorizationView('auth.oauth-authorize');

        Event::listen(Verified::class, function (Verified $event): void {
            $event->user->forceFill(['status' => AccountStatus::Active])->save();
            IdentityEvent::create([
                'user_id' => $event->user->getKey(),
                'type' => 'user.activated',
                'payload' => ['user_id' => $event->user->getKey()],
                'available_at' => now(),
            ]);
        });

        Event::listen(Login::class, function (Login $event): void {
            if ($event->user instanceof User) {
                AuditLog::create([
                    'user_id' => $event->user->getKey(),
                    'action' => 'auth.login',
                    'ip_address' => request()->ip(),
                    'user_agent' => request()->userAgent(),
                    'created_at' => now(),
                ]);
            }
        });
    }
}
