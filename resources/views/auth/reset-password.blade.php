@extends('layouts.auth')
@section('title', 'Đặt lại mật khẩu')
@section('content')
<h1>Đặt mật khẩu mới</h1>
<form method="POST" action="{{ route('password.update') }}">
    @csrf<input type="hidden" name="token" value="{{ $request->route('token') }}">
    <label>Email<input name="email" type="email" value="{{ old('email', $request->email) }}" required></label>
    <label>Mật khẩu mới<input name="password" type="password" required autocomplete="new-password"></label>
    <label>Nhập lại mật khẩu<input name="password_confirmation" type="password" required autocomplete="new-password"></label>
    <button type="submit">Đặt lại mật khẩu</button>
</form>
@endsection
