<?php

use Illuminate\Support\Facades\Route;

Route::redirect('/', '/account');

Route::view('/account', 'account')
    ->middleware(['auth', 'verified', 'account.active'])
    ->name('account');
