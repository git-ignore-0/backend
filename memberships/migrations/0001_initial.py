import django.db.models.deletion
import django.db.models.expressions
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MembershipRequest',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('short_code', models.CharField(max_length=10, unique=True)),
                ('package_name_snapshot', models.CharField(max_length=200)),
                ('package_description_snapshot', models.TextField(blank=True)),
                ('upfront_price_snapshot', models.DecimalField(decimal_places=0, max_digits=14)),
                ('duration_months_snapshot', models.PositiveSmallIntegerField()),
                ('quota_policy_snapshot', models.CharField(choices=[('expire', 'Expire'), ('rollover', 'Rollover')], max_length=16)),
                ('status', models.CharField(choices=[('consultation_requested', 'Consultation requested'), ('contacted', 'Contacted'), ('payment_pending', 'Payment pending'), ('payment_submitted', 'Payment submitted'), ('approved', 'Approved'), ('rejected', 'Rejected')], max_length=32)),
                ('open_slot', models.BooleanField(default=True, editable=False, null=True)),
                ('return_reason', models.TextField(blank=True)),
                ('rejection_reason', models.TextField(blank=True)),
                ('contacted_at', models.DateTimeField(blank=True, null=True)),
                ('payment_submitted_at', models.DateTimeField(blank=True, null=True)),
                ('returned_at', models.DateTimeField(blank=True, null=True)),
                ('approved_at', models.DateTimeField(blank=True, null=True)),
                ('rejected_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('contacted_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='membership_requests_contacted', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='MembershipUsageOrder',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('idempotency_key', models.UUIDField()),
                ('status', models.CharField(choices=[('applied', 'Applied'), ('reversed', 'Reversed')], default='applied', max_length=12)),
                ('note', models.CharField(blank=True, max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('reversed_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='membership_usage_orders_created', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='Product',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=200)),
                ('short_description', models.CharField(blank=True, max_length=800)),
                ('unit_size', models.DecimalField(decimal_places=3, max_digits=10)),
                ('unit_label_vi', models.CharField(max_length=40)),
                ('unit_label_en', models.CharField(max_length=40)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['name', 'id'],
            },
        ),
        migrations.CreateModel(
            name='ProductCategory',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=160)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['name', 'id'],
            },
        ),
        migrations.CreateModel(
            name='Membership',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('package_name_snapshot', models.CharField(max_length=200)),
                ('package_description_snapshot', models.TextField(blank=True)),
                ('upfront_price_snapshot', models.DecimalField(decimal_places=0, max_digits=14)),
                ('duration_months_snapshot', models.PositiveSmallIntegerField()),
                ('quota_policy_snapshot', models.CharField(choices=[('expire', 'Expire'), ('rollover', 'Rollover')], max_length=16)),
                ('start_date', models.DateField()),
                ('end_date', models.DateField()),
                ('status', models.CharField(choices=[('scheduled', 'Scheduled'), ('active', 'Active'), ('ended', 'Ended')], default='active', max_length=12)),
                ('current_slot', models.BooleanField(default=True, editable=False, null=True)),
                ('activated_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('activated_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='memberships_activated', to=settings.AUTH_USER_MODEL)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='memberships', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-activated_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='MembershipCycle',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sequence', models.PositiveSmallIntegerField()),
                ('start_date', models.DateField()),
                ('end_date', models.DateField()),
                ('granted_at', models.DateTimeField(auto_now_add=True)),
                ('membership', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='cycles', to='memberships.membership')),
            ],
            options={
                'ordering': ['sequence'],
            },
        ),
        migrations.CreateModel(
            name='MembershipPackage',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=200)),
                ('description', models.TextField(blank=True)),
                ('upfront_price', models.DecimalField(decimal_places=0, max_digits=14)),
                ('duration_months', models.PositiveSmallIntegerField(default=6)),
                ('quota_policy', models.CharField(choices=[('expire', 'Expire'), ('rollover', 'Rollover')], max_length=16)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['name', 'id'],
                'constraints': [models.CheckConstraint(condition=models.Q(('upfront_price__gt', 0)), name='memberships_package_price_positive'), models.CheckConstraint(condition=models.Q(('duration_months__gt', 0)), name='memberships_package_duration_positive'), models.CheckConstraint(condition=models.Q(('quota_policy__in', ['expire', 'rollover'])), name='memberships_package_policy_valid')],
            },
        ),
        migrations.AddField(
            model_name='membership',
            name='package',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='memberships', to='memberships.membershippackage'),
        ),
        migrations.CreateModel(
            name='MembershipPaymentInstruction',
            fields=[
                ('request', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, primary_key=True, related_name='payment_instruction', serialize=False, to='memberships.membershiprequest')),
                ('bank_code', models.CharField(max_length=20)),
                ('account_number', models.CharField(max_length=34)),
                ('account_name', models.CharField(max_length=160)),
                ('amount', models.DecimalField(decimal_places=0, max_digits=14)),
                ('phone_snapshot', models.CharField(max_length=32)),
                ('transfer_content', models.CharField(max_length=80)),
                ('issued_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddField(
            model_name='membershiprequest',
            name='package',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='requests', to='memberships.membershippackage'),
        ),
        migrations.AddField(
            model_name='membershiprequest',
            name='processed_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='membership_requests_processed', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='membershiprequest',
            name='user',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='membership_requests', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='membership',
            name='request',
            field=models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='membership', to='memberships.membershiprequest'),
        ),
        migrations.CreateModel(
            name='MembershipPaymentSettings',
            fields=[
                ('key', models.CharField(default='default', editable=False, max_length=20, primary_key=True, serialize=False)),
                ('bank_code', models.CharField(max_length=20)),
                ('account_number', models.CharField(max_length=34)),
                ('account_name', models.CharField(max_length=160)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='MembershipProductSnapshot',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('product_name', models.CharField(max_length=200)),
                ('unit_size', models.DecimalField(decimal_places=3, max_digits=10)),
                ('unit_label_vi', models.CharField(max_length=40)),
                ('unit_label_en', models.CharField(max_length=40)),
                ('quota_units_per_cycle', models.PositiveIntegerField()),
                ('membership', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='product_snapshots', to='memberships.membership')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='membership_snapshots', to='memberships.product')),
            ],
            options={
                'ordering': ['product_name', 'id'],
            },
        ),
        migrations.CreateModel(
            name='MembershipCycleQuota',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('granted_units', models.PositiveIntegerField()),
                ('remaining_units', models.PositiveIntegerField()),
                ('expired_units', models.PositiveIntegerField(default=0)),
                ('cycle', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='quotas', to='memberships.membershipcycle')),
                ('product_snapshot', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='cycle_quotas', to='memberships.membershipproductsnapshot')),
            ],
            options={
                'ordering': ['cycle__sequence', 'id'],
            },
        ),
        migrations.CreateModel(
            name='MembershipRequestEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('action', models.CharField(choices=[('request_created', 'Request created'), ('contacted', 'Contacted'), ('payment_instruction_issued', 'Payment instruction issued'), ('payment_submitted', 'Payment submitted'), ('payment_returned', 'Payment returned'), ('rejected', 'Rejected'), ('approved', 'Approved')], max_length=40)),
                ('reason', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='membership_request_events', to=settings.AUTH_USER_MODEL)),
                ('request', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='events', to='memberships.membershiprequest')),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.CreateModel(
            name='MembershipUsageLine',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('units', models.PositiveIntegerField()),
                ('product_snapshot', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='usage_lines', to='memberships.membershipproductsnapshot')),
            ],
        ),
        migrations.CreateModel(
            name='MembershipUsageAllocation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('units', models.PositiveIntegerField()),
                ('cycle_quota', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='usage_allocations', to='memberships.membershipcyclequota')),
                ('line', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='allocations', to='memberships.membershipusageline')),
            ],
        ),
        migrations.CreateModel(
            name='MembershipUsageReversal',
            fields=[
                ('order', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, primary_key=True, related_name='reversal', serialize=False, to='memberships.membershipusageorder')),
                ('reason', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddField(
            model_name='membershipusageorder',
            name='membership',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='usage_orders', to='memberships.membership'),
        ),
        migrations.AddField(
            model_name='membershipusageorder',
            name='user',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='membership_usage_orders', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='membershipusageline',
            name='order',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='lines', to='memberships.membershipusageorder'),
        ),
        migrations.CreateModel(
            name='MembershipRequestItemSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('product_name', models.CharField(max_length=200)),
                ('unit_size', models.DecimalField(decimal_places=3, max_digits=10)),
                ('unit_label_vi', models.CharField(max_length=40)),
                ('unit_label_en', models.CharField(max_length=40)),
                ('quota_units_per_cycle', models.PositiveIntegerField()),
                ('request', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='item_snapshots', to='memberships.membershiprequest')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='membership_request_snapshots', to='memberships.product')),
            ],
            options={
                'ordering': ['id'],
            },
        ),
        migrations.CreateModel(
            name='MembershipPackageItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quota_units', models.PositiveIntegerField()),
                ('package', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='memberships.membershippackage')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='package_items', to='memberships.product')),
            ],
            options={
                'ordering': ['id'],
            },
        ),
        migrations.AddField(
            model_name='product',
            name='category',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='products', to='memberships.productcategory'),
        ),
        migrations.AddConstraint(
            model_name='membershipcycle',
            constraint=models.UniqueConstraint(fields=('membership', 'sequence'), name='memberships_cycle_sequence_unique'),
        ),
        migrations.AddConstraint(
            model_name='membershipcycle',
            constraint=models.CheckConstraint(condition=models.Q(('sequence__gt', 0)), name='memberships_cycle_sequence_positive'),
        ),
        migrations.AddConstraint(
            model_name='membershipcycle',
            constraint=models.CheckConstraint(condition=models.Q(('end_date__gt', models.F('start_date'))), name='memberships_cycle_dates_valid'),
        ),
        migrations.AddField(
            model_name='membershippaymentinstruction',
            name='issued_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='membership_payment_instructions_issued', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddIndex(
            model_name='membershiprequest',
            index=models.Index(fields=['status', '-created_at'], name='memberships_status_5f1834_idx'),
        ),
        migrations.AddIndex(
            model_name='membershiprequest',
            index=models.Index(fields=['user', '-created_at'], name='memberships_user_id_2f354b_idx'),
        ),
        migrations.AddConstraint(
            model_name='membershiprequest',
            constraint=models.UniqueConstraint(fields=('user', 'open_slot'), name='memberships_one_open_request_per_user'),
        ),
        migrations.AddConstraint(
            model_name='membershiprequest',
            constraint=models.CheckConstraint(condition=models.Q(('upfront_price_snapshot__gt', 0)), name='memberships_request_price_positive'),
        ),
        migrations.AddConstraint(
            model_name='membershiprequest',
            constraint=models.CheckConstraint(condition=models.Q(('duration_months_snapshot__gt', 0)), name='memberships_request_duration_positive'),
        ),
        migrations.AddConstraint(
            model_name='membershiprequest',
            constraint=models.CheckConstraint(condition=models.Q(('status__in', ['consultation_requested', 'contacted', 'payment_pending', 'payment_submitted', 'approved', 'rejected'])), name='memberships_request_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='membershiprequest',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('open_slot', True), ('status__in', ['consultation_requested', 'contacted', 'payment_pending', 'payment_submitted'])), models.Q(('open_slot__isnull', True), ('status__in', ['approved', 'rejected'])), _connector='OR'), name='memberships_request_open_slot_consistent'),
        ),
        migrations.AddConstraint(
            model_name='membership',
            constraint=models.UniqueConstraint(fields=('user', 'current_slot'), name='memberships_one_current_per_user'),
        ),
        migrations.AddConstraint(
            model_name='membership',
            constraint=models.CheckConstraint(condition=models.Q(('end_date__gt', models.F('start_date'))), name='memberships_dates_valid'),
        ),
        migrations.AddConstraint(
            model_name='membership',
            constraint=models.CheckConstraint(condition=models.Q(('status__in', ['scheduled', 'active', 'ended'])), name='memberships_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='membership',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('current_slot', True), ('status__in', ['scheduled', 'active'])), models.Q(('current_slot__isnull', True), ('status', 'ended')), _connector='OR'), name='memberships_current_slot_consistent'),
        ),
        migrations.AddConstraint(
            model_name='membershippaymentsettings',
            constraint=models.CheckConstraint(condition=models.Q(('key', 'default')), name='memberships_payment_settings_singleton_key'),
        ),
        migrations.AddConstraint(
            model_name='membershipcyclequota',
            constraint=models.UniqueConstraint(fields=('cycle', 'product_snapshot'), name='memberships_cycle_product_unique'),
        ),
        migrations.AddConstraint(
            model_name='membershipcyclequota',
            constraint=models.CheckConstraint(condition=models.Q(('granted_units__gt', 0)), name='memberships_cycle_granted_positive'),
        ),
        migrations.AddConstraint(
            model_name='membershipcyclequota',
            constraint=models.CheckConstraint(condition=models.Q(('remaining_units__gte', 0)), name='memberships_cycle_remaining_nonnegative'),
        ),
        migrations.AddConstraint(
            model_name='membershipcyclequota',
            constraint=models.CheckConstraint(condition=models.Q(('remaining_units__lte', django.db.models.expressions.CombinedExpression(models.F('granted_units'), '-', models.F('expired_units')))), name='memberships_cycle_quota_totals_valid'),
        ),
        migrations.AddIndex(
            model_name='membershiprequestevent',
            index=models.Index(fields=['request', 'created_at'], name='memberships_request_965db3_idx'),
        ),
        migrations.AddConstraint(
            model_name='membershiprequestevent',
            constraint=models.CheckConstraint(condition=models.Q(('action__in', ['request_created', 'contacted', 'payment_instruction_issued', 'payment_submitted', 'payment_returned', 'rejected', 'approved'])), name='memberships_request_event_action_valid'),
        ),
        migrations.AddConstraint(
            model_name='membershipusageallocation',
            constraint=models.UniqueConstraint(fields=('line', 'cycle_quota'), name='memberships_usage_allocation_unique'),
        ),
        migrations.AddConstraint(
            model_name='membershipusageallocation',
            constraint=models.CheckConstraint(condition=models.Q(('units__gt', 0)), name='memberships_allocation_units_positive'),
        ),
        migrations.AddField(
            model_name='membershipusagereversal',
            name='actor',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='membership_usage_reversals', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddIndex(
            model_name='membershipusageorder',
            index=models.Index(fields=['membership', '-created_at'], name='memberships_members_f5eb13_idx'),
        ),
        migrations.AddIndex(
            model_name='membershipusageorder',
            index=models.Index(fields=['user', '-created_at'], name='memberships_user_id_6c27b3_idx'),
        ),
        migrations.AddConstraint(
            model_name='membershipusageorder',
            constraint=models.UniqueConstraint(fields=('membership', 'idempotency_key'), name='memberships_usage_idempotency_unique'),
        ),
        migrations.AddConstraint(
            model_name='membershipusageorder',
            constraint=models.CheckConstraint(condition=models.Q(('status__in', ['applied', 'reversed'])), name='memberships_usage_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='membershipusageorder',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('reversed_at__isnull', True), ('status', 'applied')), models.Q(('reversed_at__isnull', False), ('status', 'reversed')), _connector='OR'), name='memberships_usage_reversal_state_consistent'),
        ),
        migrations.AddConstraint(
            model_name='membershipusageline',
            constraint=models.UniqueConstraint(fields=('order', 'product_snapshot'), name='memberships_usage_product_unique'),
        ),
        migrations.AddConstraint(
            model_name='membershipusageline',
            constraint=models.CheckConstraint(condition=models.Q(('units__gt', 0)), name='memberships_usage_units_positive'),
        ),
        migrations.AddConstraint(
            model_name='membershiprequestitemsnapshot',
            constraint=models.UniqueConstraint(fields=('request', 'product'), name='memberships_request_snapshot_product_unique'),
        ),
        migrations.AddConstraint(
            model_name='membershiprequestitemsnapshot',
            constraint=models.CheckConstraint(condition=models.Q(('unit_size__gt', 0)), name='memberships_request_snapshot_unit_positive'),
        ),
        migrations.AddConstraint(
            model_name='membershiprequestitemsnapshot',
            constraint=models.CheckConstraint(condition=models.Q(('quota_units_per_cycle__gt', 0)), name='memberships_request_snapshot_quota_positive'),
        ),
        migrations.AddConstraint(
            model_name='membershipproductsnapshot',
            constraint=models.UniqueConstraint(fields=('membership', 'product'), name='memberships_snapshot_product_unique'),
        ),
        migrations.AddConstraint(
            model_name='membershipproductsnapshot',
            constraint=models.CheckConstraint(condition=models.Q(('unit_size__gt', 0)), name='memberships_snapshot_unit_size_positive'),
        ),
        migrations.AddConstraint(
            model_name='membershipproductsnapshot',
            constraint=models.CheckConstraint(condition=models.Q(('quota_units_per_cycle__gt', 0)), name='memberships_snapshot_quota_positive'),
        ),
        migrations.AddConstraint(
            model_name='membershippackageitem',
            constraint=models.UniqueConstraint(fields=('package', 'product'), name='memberships_package_product_unique'),
        ),
        migrations.AddConstraint(
            model_name='membershippackageitem',
            constraint=models.CheckConstraint(condition=models.Q(('quota_units__gt', 0)), name='memberships_package_item_quota_positive'),
        ),
        migrations.AddConstraint(
            model_name='product',
            constraint=models.CheckConstraint(condition=models.Q(('unit_size__gt', 0)), name='memberships_product_unit_size_positive'),
        ),
        migrations.AddConstraint(
            model_name='membershippaymentinstruction',
            constraint=models.CheckConstraint(condition=models.Q(('amount__gt', 0)), name='memberships_instruction_amount_positive'),
        ),
    ]
