@extends('layouts.auth')
@section('title', 'Tạo tài khoản')
@section('content')
<h1>Tạo tài khoản chung</h1>
<p class="lead">Đăng ký một lần để dùng cho cả website và diễn đàn.</p>
<form method="POST" action="{{ route('register') }}">
    @csrf
    <input type="hidden" name="locale" value="{{ app()->getLocale() }}">
    <label>Họ và tên<input name="name" value="{{ old('name') }}" required autofocus autocomplete="name"></label>
    <label>Email<input name="email" type="email" value="{{ old('email') }}" required autocomplete="email"></label>
    <label>Mật khẩu<input name="password" type="password" required autocomplete="new-password"><small>Tối thiểu 12 ký tự, có chữ hoa, chữ thường và số.</small></label>
    <label>Nhập lại mật khẩu<input name="password_confirmation" type="password" required autocomplete="new-password"></label>
    <button type="submit">Tạo tài khoản</button>
</form>
<div class="links"><a href="{{ route('login') }}">Đã có tài khoản? Đăng nhập</a></div>
@endsection
