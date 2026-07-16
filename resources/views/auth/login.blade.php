@extends('layouts.auth')
@section('title', 'Đăng nhập')
@section('content')
<h1>Chào mừng trở lại</h1>
<p class="lead">Một tài khoản cho website Natural Farming Vietnam và diễn đàn.</p>
<form method="POST" action="{{ route('login') }}">
    @csrf
    <label>Email<input name="email" type="email" value="{{ old('email') }}" required autofocus autocomplete="email"></label>
    <label>Mật khẩu<input name="password" type="password" required autocomplete="current-password"></label>
    <label class="check"><input name="remember" type="checkbox"> Ghi nhớ đăng nhập</label>
    <button type="submit">Đăng nhập</button>
</form>
<div class="links"><a href="{{ route('password.request') }}">Quên mật khẩu?</a><a href="{{ route('register') }}">Tạo tài khoản</a></div>
@endsection
