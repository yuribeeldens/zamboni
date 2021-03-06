# -*- coding: utf-8 -*-
import datetime

from django.conf import settings
from django.contrib.auth.models import User
from django.test.client import RequestFactory

import mock
from nose.tools import eq_
from pyquery import PyQuery as pq

from access.models import GroupUser
from addons.models import Persona
import amo
import amo.tests
from amo.tests import addon_factory, days_ago
from amo.urlresolvers import reverse
from devhub.models import ActivityLog
from editors.models import RereviewQueueTheme
import mkt.constants.reviewers as rvw
from mkt.reviewers.models import ThemeLock
from mkt.reviewers.views_themes import _get_themes
from mkt.site.fixtures import fixture
from users.models import UserProfile


class ThemeReviewTestMixin(object):
    fixtures = fixture('group_admin', 'user_admin', 'user_admin_group',
                       'user_persona_reviewer', 'user_999',
                       'user_senior_persona_reviewer')

    def setUp(self):
        self.reviewer_count = 0
        self.create_switch(name='mkt-themes')
        self.status = amo.STATUS_PENDING
        self.flagged = False
        self.rereview = False

    def req_factory_factory(self, user, url):
        req = RequestFactory().get(reverse(url))
        req.user = user.user
        req.groups = req.user.get_profile().groups.all()
        req.TABLET = True
        return req

    def create_and_become_reviewer(self):
        """Login as new reviewer with unique username."""
        username = 'reviewer%s' % self.reviewer_count
        email = username + '@mozilla.com'
        reviewer = User.objects.create(username=email, email=email,
                                       is_active=True, is_superuser=True)
        user = UserProfile.objects.create(user=reviewer, email=email,
                                          username=username)
        user.set_password('password')
        user.save()
        GroupUser.objects.create(group_id=50060, user=user)

        self.client.login(username=email, password='password')
        self.reviewer_count += 1
        return user

    def theme_factory(self, status=None):
        status = status or self.status
        addon = addon_factory(type=amo.ADDON_PERSONA, status=status)
        if self.rereview:
            RereviewQueueTheme.objects.create(
                theme=addon.persona, header='pending_header',
                footer='pending_footer')
        persona = addon.persona
        persona.header = 'header'
        persona.footer = 'footer'
        persona.save()
        return addon

    def get_themes(self, reviewer):
        return _get_themes(mock.Mock(), reviewer, flagged=self.flagged,
                           rereview=self.rereview)

    @mock.patch.object(rvw, 'THEME_INITIAL_LOCKS', 2)
    def test_basic_queue(self):
        """
        Have reviewers take themes from the pool,
        check their queue sizes.
        """
        for x in range(rvw.THEME_INITIAL_LOCKS + 1):
            self.theme_factory()

        expected_themes = []
        if self.rereview:
            rrq = RereviewQueueTheme.objects.all()
            expected_themes = [
                [rrq[0], rrq[1]],
                [rrq[2]],
                []
            ]
        else:
            themes = Persona.objects.all()
            expected_themes = [
                [themes[0], themes[1]],
                [themes[2]],
                []
            ]

        for expected in expected_themes:
            reviewer = self.create_and_become_reviewer()
            self.assertSetEqual(self.get_themes(reviewer), expected)
            eq_(ThemeLock.objects.filter(reviewer=reviewer).count(),
                len(expected))

    @mock.patch.object(settings, 'LOCAL_MIRROR_URL', '')
    @mock.patch('mkt.reviewers.tasks.send_mail_jinja')
    @mock.patch('mkt.reviewers.tasks.create_persona_preview_images')
    @mock.patch('amo.storage_utils.copy_stored_file')
    def test_commit(self, copy_file_mock, create_preview_mock,
                    send_mail_jinja_mock):
        if self.flagged:
            return
        themes = []
        for x in range(5):
            themes.append(self.theme_factory().persona)
        form_data = amo.tests.formset(initial_count=5, total_count=6)

        # Create locks.
        reviewer = self.create_and_become_reviewer()
        for index, theme in enumerate(themes):
            ThemeLock.objects.create(
                theme=theme, reviewer=reviewer,
                expiry=datetime.datetime.now() +
                datetime.timedelta(minutes=rvw.THEME_LOCK_EXPIRY))
            form_data['form-%s-theme' % index] = str(theme.id)

        # Build formset.
        actions = (
            (str(rvw.ACTION_MOREINFO), 'moreinfo', ''),
            (str(rvw.ACTION_FLAG), 'flag', ''),
            (str(rvw.ACTION_DUPLICATE), 'duplicate', ''),
            (str(rvw.ACTION_REJECT), 'reject', '1'),
            (str(rvw.ACTION_APPROVE), '', ''),
        )
        for index, action in enumerate(actions):
            action, comment, reject_reason = action
            form_data['form-%s-action' % index] = action
            form_data['form-%s-comment' % index] = comment
            form_data['form-%s-reject_reason' % index] = reject_reason

        # Commit.
        res = self.client.post(reverse('reviewers.themes.commit'), form_data)
        self.assert3xx(res, reverse('reviewers.themes.queue_themes'))

        if self.rereview:
            # Original design of reuploaded themes should stay public.
            for i in range(4):
                eq_(themes[i].addon.status, amo.STATUS_PUBLIC)
                eq_(themes[i].header, 'header')
                eq_(themes[i].footer, 'footer')

            assert '/pending_header' in copy_file_mock.call_args_list[0][0][0]
            assert '/header' in copy_file_mock.call_args_list[0][0][1]
            assert '/pending_footer' in copy_file_mock.call_args_list[1][0][0]
            assert '/footer' in copy_file_mock.call_args_list[1][0][1]

            create_preview_args = create_preview_mock.call_args_list[0][1]
            assert '/header' in create_preview_args['src']
            assert '/preview' in create_preview_args['full_dst'][0]
            assert '/icon' in create_preview_args['full_dst'][1]

            # Only two since reuploaded themes are not flagged/moreinfo'ed.
            eq_(RereviewQueueTheme.objects.count(), 2)
        else:
            eq_(themes[0].addon.status, amo.STATUS_REVIEW_PENDING)
            eq_(themes[1].addon.status, amo.STATUS_REVIEW_PENDING)
            eq_(themes[2].addon.status, amo.STATUS_REJECTED)
            eq_(themes[3].addon.status, amo.STATUS_REJECTED)
        eq_(themes[4].addon.status, amo.STATUS_PUBLIC)
        eq_(ActivityLog.objects.count(), 3 if self.rereview else 5)

        expected_calls = [
            mock.call(
                'A question about your Theme submission',
                'reviewers/themes/emails/moreinfo.html',
                {'reason': None,
                 'comment': u'moreinfo',
                 'theme': themes[0],
                 'reviewer_email': u'reviewer0@mozilla.com',
                 'base_url': 'http://testserver'},
                headers={'Reply-To': settings.THEMES_EMAIL},
                from_email=settings.ADDONS_EMAIL,
                recipient_list=set([])),
            mock.call(
                'Theme submission flagged for review',
                'reviewers/themes/emails/flag_reviewer.html',
                {'reason': None,
                 'comment': u'flag',
                 'theme': themes[1],
                 'base_url': 'http://testserver'},
                headers={'Reply-To': settings.THEMES_EMAIL},
                from_email=settings.ADDONS_EMAIL,
                recipient_list=[settings.THEMES_EMAIL]),
            mock.call(
                'A problem with your Theme submission',
                'reviewers/themes/emails/reject.html',
                {'reason': mock.ANY,
                 'comment': u'duplicate',
                 'theme': themes[2],
                 'base_url': 'http://testserver'},
                headers={'Reply-To': settings.THEMES_EMAIL},
                from_email=settings.ADDONS_EMAIL,
                recipient_list=set([])),
            mock.call(
                'A problem with your Theme submission',
                'reviewers/themes/emails/reject.html',
                {'reason': mock.ANY,
                 'comment': u'reject',
                 'theme': themes[3],
                 'base_url': 'http://testserver'},
                headers={'Reply-To': settings.THEMES_EMAIL},
                from_email=settings.ADDONS_EMAIL,
                recipient_list=set([])),
            mock.call(
                'Thanks for submitting your Theme',
                'reviewers/themes/emails/approve.html',
                {'reason': None,
                 'comment': u'',
                 'theme': themes[4],
                 'base_url': 'http://testserver'},
                headers={'Reply-To': settings.THEMES_EMAIL},
                from_email=settings.ADDONS_EMAIL,
                recipient_list=set([]))
        ]
        if self.rereview:
            eq_(send_mail_jinja_mock.call_args_list[0], expected_calls[2])
            eq_(send_mail_jinja_mock.call_args_list[1], expected_calls[3])
            eq_(send_mail_jinja_mock.call_args_list[2], expected_calls[4])
        else:
            eq_(send_mail_jinja_mock.call_args_list[0], expected_calls[0])
            eq_(send_mail_jinja_mock.call_args_list[1], expected_calls[1])
            eq_(send_mail_jinja_mock.call_args_list[2], expected_calls[2])
            eq_(send_mail_jinja_mock.call_args_list[3], expected_calls[3])
            eq_(send_mail_jinja_mock.call_args_list[4], expected_calls[4])

    def test_single_basic(self):
        with self.settings(ALLOW_SELF_REVIEWS=True):
            user = UserProfile.objects.get(
                email='persona_reviewer@mozilla.com')
            self.login(user)
            addon = self.theme_factory()

            res = self.client.get(reverse('reviewers.themes.single',
                                          args=[addon.slug]))
            eq_(res.status_code, 200)
            eq_(res.context['theme'].id,
                addon.persona.rereviewqueuetheme_set.all()[0].id
                if self.rereview else addon.persona.id)
            eq_(res.context['reviewable'], not self.flagged)


class TestThemeQueue(ThemeReviewTestMixin, amo.tests.TestCase):

    def setUp(self):
        super(TestThemeQueue, self).setUp()
        self.queue_url = reverse('reviewers.themes.queue_themes')

    def check_permissions(self, slug, status_code):
        for url in [reverse('reviewers.themes.queue_themes'),
                    reverse('reviewers.themes.single', args=[slug])]:
            eq_(self.client.get(url).status_code, status_code)

    def test_permissions_reviewer(self):
        slug = self.theme_factory().slug

        self.assertLoginRedirects(self.client.get(self.queue_url),
                                  self.queue_url)

        self.login('regular@mozilla.com')
        self.check_permissions(slug, 403)

        self.create_and_become_reviewer()
        self.check_permissions(slug, 200)

    def test_can_review_your_app(self):
        with self.settings(ALLOW_SELF_REVIEWS=False):
            user = UserProfile.objects.get(
                email='persona_reviewer@mozilla.com')
            self.login(user)
            addon = self.theme_factory()

            res = self.client.get(self.queue_url)
            eq_(len(res.context['theme_formsets']), 1)
            # I should be able to review this app. It is not mine.
            eq_(res.context['theme_formsets'][0][0], addon.persona)

    def test_cannot_review_my_app(self):
        with self.settings(ALLOW_SELF_REVIEWS=False):
            user = UserProfile.objects.get(
                email='persona_reviewer@mozilla.com')
            self.login(user)
            addon = self.theme_factory()

            addon.addonuser_set.create(user=user)

            res = self.client.get(self.queue_url)
            # I should not be able to review my own app.
            eq_(len(res.context['theme_formsets']), 0)

    def test_theme_list(self):
        self.create_and_become_reviewer()
        self.theme_factory()
        res = self.client.get(reverse('reviewers.themes.list'))
        eq_(res.status_code, 200)
        eq_(pq(res.content)('#addon-queue tbody tr').length, 1)

    @mock.patch.object(rvw, 'THEME_INITIAL_LOCKS', 1)
    def test_release_locks(self):
        for x in range(2):
            addon_factory(type=amo.ADDON_PERSONA, status=self.status)
        other_reviewer = self.create_and_become_reviewer()
        _get_themes(mock.Mock(), other_reviewer)

        # Check reviewer's theme lock released.
        reviewer = self.create_and_become_reviewer()
        _get_themes(mock.Mock(), reviewer)
        eq_(ThemeLock.objects.filter(reviewer=reviewer).count(), 1)
        self.client.get(reverse('reviewers.themes.release_locks'))
        eq_(ThemeLock.objects.filter(reviewer=reviewer).count(), 0)

        # Check other reviewer's theme lock intact.
        eq_(ThemeLock.objects.filter(reviewer=other_reviewer).count(), 1)

    @mock.patch.object(rvw, 'THEME_INITIAL_LOCKS', 2)
    def test_themes_less_than_initial(self):
        """
        Number of themes in the pool is less than amount we want to check out.
        """
        addon_factory(type=amo.ADDON_PERSONA, status=self.status)
        reviewer = self.create_and_become_reviewer()
        eq_(len(_get_themes(mock.Mock(), reviewer)), 1)
        eq_(len(_get_themes(mock.Mock(), reviewer)), 1)

    @mock.patch.object(rvw, 'THEME_INITIAL_LOCKS', 2)
    def test_top_off(self):
        """If reviewer has fewer than max locks, get more from pool."""
        for x in range(2):
            self.theme_factory()
        reviewer = self.create_and_become_reviewer()
        self.get_themes(reviewer)
        ThemeLock.objects.filter(reviewer=reviewer)[0].delete()
        self.get_themes(reviewer)

        # Check reviewer checked out the themes.
        eq_(ThemeLock.objects.filter(reviewer=reviewer).count(),
            rvw.THEME_INITIAL_LOCKS)

    @mock.patch.object(rvw, 'THEME_INITIAL_LOCKS', 2)
    def test_expiry(self):
        """
        Test that reviewers who want themes from an empty pool can steal
        checked-out themes from other reviewers whose locks have expired.
        """
        for x in range(2):
            self.theme_factory(status=self.status)
        reviewer = self.create_and_become_reviewer()
        self.get_themes(reviewer)

        # Reviewer wants themes, but empty pool.
        reviewer = self.create_and_become_reviewer()
        self.get_themes(reviewer)
        eq_(ThemeLock.objects.filter(reviewer=reviewer).count(), 0)

        # Manually expire a lock and see if it's reassigned.
        expired_theme_lock = ThemeLock.objects.all()[0]
        expired_theme_lock.expiry = self.days_ago(1)
        expired_theme_lock.save()
        self.get_themes(reviewer)
        eq_(ThemeLock.objects.filter(reviewer=reviewer).count(), 1)

    def test_expiry_update(self):
        """Test expiry is updated when reviewer reloads his queue."""
        self.theme_factory()
        reviewer = self.create_and_become_reviewer()
        self.get_themes(reviewer)

        ThemeLock.objects.filter(reviewer=reviewer).update(expiry=days_ago(1))
        _get_themes(mock.Mock(), reviewer, flagged=self.flagged)
        self.get_themes(reviewer)
        eq_(ThemeLock.objects.filter(reviewer=reviewer)[0].expiry >
            days_ago(1), True)

    def test_user_review_history(self):
        self.theme_factory()

        reviewer = self.create_and_become_reviewer()

        res = self.client.get(reverse('reviewers.themes.history'))
        eq_(res.status_code, 200)
        doc = pq(res.content)
        eq_(doc('tbody tr').length, 0)

        theme = Persona.objects.all()[0]
        for x in range(3):
            amo.log(amo.LOG.THEME_REVIEW, theme.addon, user=reviewer,
                    details={'action': rvw.ACTION_APPROVE,
                             'comment': '', 'reject_reason': ''})

        res = self.client.get(reverse('reviewers.themes.history'))
        eq_(res.status_code, 200)
        doc = pq(res.content)
        eq_(doc('tbody tr').length, 3)

        res = self.client.get(reverse('reviewers.themes.logs'))
        eq_(res.status_code, 200)
        doc = pq(res.content)
        eq_(doc('tbody tr').length, 3 * 2)  # Double for comment rows.

    def test_single_cannot_review_my_app(self):
        with self.settings(ALLOW_SELF_REVIEWS=False):
            user = UserProfile.objects.get(
                email='persona_reviewer@mozilla.com')
            self.login(user)
            addon = self.theme_factory()

            addon.addonuser_set.create(user=user)

            res = self.client.get(reverse('reviewers.themes.single',
                                          args=[addon.slug]))
            eq_(res.status_code, 200)
            eq_(res.context['theme'].id,
                addon.persona.rereviewqueuetheme_set.all()[0].id
                if self.rereview else addon.persona.id)
            eq_(res.context['reviewable'], False)


class TestThemeQueueFlagged(ThemeReviewTestMixin, amo.tests.TestCase):

    def setUp(self):
        super(TestThemeQueueFlagged, self).setUp()
        self.status = amo.STATUS_REVIEW_PENDING
        self.flagged = True
        self.queue_url = reverse('reviewers.themes.queue_flagged')

    def test_admin_only(self):
        self.login('persona_reviewer@mozilla.com')
        eq_(self.client.get(self.queue_url).status_code, 403)

        self.login('senior_persona_reviewer@mozilla.com')
        eq_(self.client.get(self.queue_url).status_code, 200)


class TestThemeQueueRereview(ThemeReviewTestMixin, amo.tests.TestCase):

    def setUp(self):
        super(TestThemeQueueRereview, self).setUp()
        self.status = amo.STATUS_PUBLIC
        self.rereview = True
        self.queue_url = reverse('reviewers.themes.queue_rereview')


class TestDeletedThemeLookup(amo.tests.TestCase):
    fixtures = fixture('group_admin', 'user_admin', 'user_admin_group',
                       'user_persona_reviewer', 'user_senior_persona_reviewer')

    def setUp(self):
        self.deleted = addon_factory(type=amo.ADDON_PERSONA)
        self.deleted.update(status=amo.STATUS_DELETED)
        self.create_switch(name='mkt-themes')

    def test_table(self):
        self.client.login(username='senior_persona_reviewer@mozilla.com',
                          password='password')
        r = self.client.get(reverse('reviewers.themes.deleted'))
        eq_(r.status_code, 200)
        eq_(pq(r.content)('tbody td:nth-child(3)').text(),
            self.deleted.name.localized_string)

    def test_perm(self):
        self.client.login(username='persona_reviewer@mozilla.com',
                          password='password')
        r = self.client.get(reverse('reviewers.themes.deleted'))
        eq_(r.status_code, 403)
