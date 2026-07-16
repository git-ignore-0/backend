<?php

use App\Http\Controllers\Api\MeController;
use Illuminate\Support\Facades\Route;

Route::middleware(['auth:api', 'account.active'])->prefix('v1')->group(function (): void {
    Route::get('/me', MeController::class)->middleware('scope:profile,email')->name('api.me');
});
